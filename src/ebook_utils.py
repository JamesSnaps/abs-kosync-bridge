import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString, Tag
import hashlib
import logging
import os
import re
import glob # <--- Added import for escaping
import rapidfuzz
from pathlib import Path
from rapidfuzz import process, fuzz
from fuzzysearch import find_near_matches

logger = logging.getLogger(__name__)

XPOINTER_RE = re.compile(
    r'^/body/DocFragment\[(?P<fragment>\d+)\]/body'
    r'(?P<path>(?:/[\w:-]+(?:\[\d+\])?)*)'
    r'(?P<text>/text\(\)(?:\[(?P<text_position>\d+)\])?)?'
    r'(?:\.(?P<offset>\d+))?$'
)
XPOINTER_STEP_RE = re.compile(r'/([\w:-]+)(?:\[(\d+)\])?')
XPOINTER_MARK = ''
POSITION_TEXT_LENGTH = 900

class EbookParser:
    def __init__(self, books_dir):
        self.books_dir = books_dir
        self.cache = {} 
        self.normalized_cache = {}
        self.lower_cache = {}
        self.sentence_cache = {}
        # Stores metadata about where chapters begin/end in the full text
        self.spine_maps = {} 
        
        self.fuzzy_threshold = int(os.getenv("FUZZY_MATCH_THRESHOLD", 80))
        self.hash_method = os.getenv("KOSYNC_HASH_METHOD", "content").lower()
        logger.info(f"Initialized EbookParser. ID Method: {self.hash_method}")

    def _resolve_book_path(self, filename):
        """
        Robustly finds a file in the books directory, handling special characters
        like [ ] (brackets) which break standard glob patterns.
        """
        # 1. Try Glob with escaping (Fastest)
        try:
            # Escape brackets, etc.
            safe_name = glob.escape(filename) 
            return next(self.books_dir.glob(f"**/{safe_name}"))
        except StopIteration:
            pass
            
        # 2. Fallback: Linear scan (Slower, but 100% reliable)
        # This catches edge cases where glob.escape might behave differently on OS versions
        for f in self.books_dir.rglob("*"):
            if f.name == filename:
                return f
        
        raise FileNotFoundError(f"Could not locate {filename}")

    def get_kosync_id(self, filepath):
        filepath = Path(filepath)
        if self.hash_method == "filename":
            return self._compute_filename_hash(filepath)
        return self._compute_koreader_hash(filepath)

    def _compute_filename_hash(self, filepath):
        return hashlib.md5(filepath.name.encode('utf-8')).hexdigest()

    def _compute_koreader_hash(self, filepath):
        md5 = hashlib.md5()
        try:
            file_size = os.path.getsize(filepath)
            with open(filepath, 'rb') as f:
                for i in range(-1, 11): 
                    if i == -1: offset = 0
                    else: offset = 1024 * (4 ** i)
                    if offset >= file_size: break
                    f.seek(offset)
                    chunk = f.read(1024)
                    if not chunk: break   
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            logger.error(f"Error computing hash: {e}")
            return None

    def extract_text_and_map(self, filepath):
        filepath = Path(filepath)
        if str(filepath) in self.cache:
            return self.cache[str(filepath)], self.spine_maps[str(filepath)]

        logger.info(f"Parsing ebook structure: {filepath.name}")
        try:
            book = epub.read_epub(str(filepath))
            full_text_parts = []
            spine_map = [] 
            
            current_idx = 0
            
            for i, item_ref in enumerate(book.spine):
                item = book.get_item_with_id(item_ref[0])
                if item.get_type() == ebooklib.ITEM_DOCUMENT:
                    soup = BeautifulSoup(item.get_content(), 'html.parser')
                    text = soup.get_text(separator=' ', strip=True)
                    
                    start = current_idx
                    length = len(text)
                    end = current_idx + length
                    
                    spine_map.append({
                        "start": start,
                        "end": end,
                        "spine_index": i + 1, 
                        "content": item.get_content() 
                    })
                    
                    full_text_parts.append(text)
                    current_idx = end + 1 
            
            combined_text = " ".join(full_text_parts)
            self.cache[str(filepath)] = combined_text
            self.spine_maps[str(filepath)] = spine_map
            
            return combined_text, spine_map
            
        except Exception as e:
            logger.error(f"Failed to parse EPUB {filepath}: {e}")
            return "", []

    def _generate_xpath(self, html_content, local_target_index):
        soup = BeautifulSoup(html_content, 'html.parser')
        current_char_count = 0
        target_tag = None
        
        elements = soup.find_all(string=True)
        for string in elements:
            text_len = len(string.strip())
            if text_len == 0: continue
            
            if current_char_count + text_len >= local_target_index:
                target_tag = string.parent
                break
            
            current_char_count += text_len
            if current_char_count < local_target_index:
                current_char_count += 1
        
        if not target_tag:
            return "/body/div/p[1]"

        path_segments = []
        curr = target_tag
        while curr and curr.name != '[document]':
            if curr.name == 'body':
                path_segments.append("body")
                break
            
            index = 1
            sibling = curr.previous_sibling
            while sibling:
                if isinstance(sibling, Tag) and sibling.name == curr.name:
                    index += 1
                sibling = sibling.previous_sibling
            
            segment = f"{curr.name}[{index}]"
            path_segments.append(segment)
            curr = curr.parent
            
        return "/" + "/".join(reversed(path_segments))

    def _normalize(self, text):
        return re.sub(r'[^a-z0-9]', '', text.lower())

    def find_text_location(self, filename, search_phrase):
        try:
            # FIXED: Use the new robust path resolver
            book_path = self._resolve_book_path(filename)
            
            full_text, spine_map = self.extract_text_and_map(book_path)
            
            if not full_text: return None, None, None

            total_len = len(full_text)
            match_index = -1

            # 1. Exact Match
            match_index = full_text.find(search_phrase)
            
            # 2. Normalized Match
            if match_index == -1:
                logger.info("  ...Exact match failed. Trying Normalized match...")
                cache_key = str(filename)
                if cache_key not in self.normalized_cache:
                    self.normalized_cache[cache_key] = self._normalize(full_text)
                
                norm_content = self.normalized_cache[cache_key]
                norm_search = self._normalize(search_phrase)
                norm_index = norm_content.find(norm_search)

                if norm_index != -1:
                    logger.info("  ✅ Normalized match successful.")
                    match_index = int((norm_index / len(norm_content)) * total_len)

            # # 3. Fuzzy Match
            # if match_index == -1:
            #     logger.info(f"  ...Normalized failed. Trying Fuzzy Match...")
            #     cache_key = str(filename)
            #     if cache_key not in self.sentence_cache:
            #         self.sentence_cache[cache_key] = full_text.split('. ')
                
            #     sentences = self.sentence_cache[cache_key]
            #     match = process.extractOne(search_phrase, sentences, scorer=fuzz.token_set_ratio)
                
            #     if match:
            #         matched_string, score, _ = match
            #         if score >= self.fuzzy_threshold:
            #             logger.info(f"  ✅ Fuzzy match successful (Score: {score:.1f}).")
            #             match_index = full_text.find(matched_string)
           
            # # 3. Fuzzy Match (Revised)
            # if match_index == -1:
            #     logger.info("  ...Normalized failed. Trying Fuzzy Match with Levenshtein distance...")
                
            #     max_errors = int(len(search_phrase) * 0.2) # Allow 20% error rate
                
            #     matches = find_near_matches(search_phrase, full_text, max_l_dist=max_errors)
            
            #     if matches:
            #         # Get the best match (lowest distance / errors)
            #         best_match = min(matches, key=lambda x: x.dist)
                    
            #         logger.info(f"  ✅ Fuzzy match successful (Dist: {best_match.dist}).")
            #         match_index = best_match.start            

            # 3. Fuzzy Match (RapidFuzz Optimized)
            if match_index == -1:
                logger.info("  ...Normalized failed. Trying Fuzzy Match with RapidFuzz...")

                # RapidFuzz uses a 0-100 score. 
                # ~75 is roughly equivalent to allowing 20-25% errors.
                cutoff_score = 75 

                # partial_ratio_alignment finds the best alignment of the search_phrase
                # within the full_text.
                # Returns an object with: score, src_start, src_end, dest_start, dest_end
                #
                # Compared lowercased: books set chat logs, system messages and
                # emphasis in capitals that the transcript has in normal case
                # (Dungeon Crawler Carl scored 70 on such a passage, 98+ either
                # side of it). Same length, so dest_start indexes full_text.
                cache_key = str(filename)
                if cache_key not in self.lower_cache:
                    lowered = full_text.lower()
                    self.lower_cache[cache_key] = lowered if len(lowered) == total_len else full_text
                alignment = rapidfuzz.fuzz.partial_ratio_alignment(
                    search_phrase.lower(),
                    self.lower_cache[cache_key],
                    score_cutoff=cutoff_score
                )

                if alignment:
                    logger.info(f"  ✅ Fuzzy match successful (Score: {alignment.score:.1f}).")
                    # 'dest_start' is the index where the match starts in full_text
                    match_index = alignment.dest_start
            
            if match_index != -1:
                percentage = match_index / total_len
                xpath = None
                for item in spine_map:
                    if item['start'] <= match_index < item['end']:
                        local_index = match_index - item['start']
                        dom_path = self._generate_xpath(item['content'], local_index)
                        xpath = f"/body/DocFragment[{item['spine_index']}]{dom_path}"
                        logger.info(f"  📍 Generated XPath: {xpath}")
                        break
                
                return percentage, xpath, match_index
            
            return None, None, None

        except FileNotFoundError:
            logger.error(f"  Book file not found: {filename}")
            return None, None, None
        except Exception as e:
            logger.error(f"  Error finding location in {filename}: {e}")
            return None, None, None

    def get_text_at_percentage(self, filename, percentage):
        try:
            # FIXED: Use the new robust path resolver
            book_path = self._resolve_book_path(filename)
            
            full_text, _ = self.extract_text_and_map(book_path)
            
            if not full_text: return None
            
            total_len = len(full_text)
            target_index = int(total_len * percentage)
            
            start = max(0, target_index - 450)
            end = min(total_len, target_index + 450)
            
            return full_text[start:end]
        except FileNotFoundError:
            logger.error(f"  Book file not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"  Error extracting text from {filename}: {e}")
            return None

    def _xpointer_to_index(self, spine_map, xpointer):
        """Offset of a KOReader xpointer in the text from extract_text_and_map, or None.

        Handles what KOReader sends (.../p[50]/text().655) and the element-only
        paths _generate_xpath writes back (.../p[75]/b[1]).
        """
        match = XPOINTER_RE.match(xpointer)
        if not match:
            return None

        item = next((s for s in spine_map if s['spine_index'] == int(match['fragment'])), None)
        if item is None:
            return None

        soup = BeautifulSoup(item['content'], 'html.parser')
        try:
            node = soup.find('body')
            for name, position in XPOINTER_STEP_RE.findall(match['path']):
                node = node.find_all(name, recursive=False)[int(position or 1) - 1]

            # Mark the spot and search for it, so the offset is measured in exactly
            # the text extract_text_and_map produces.
            if match['text']:
                texts = [c for c in node.children if type(c) is NavigableString and c.strip()]
                text = texts[int(match['text_position'] or 1) - 1]
                offset = int(match['offset'] or 0)
                text.replace_with(text[:offset] + XPOINTER_MARK + text[offset:])
            else:
                node.insert(0, XPOINTER_MARK)
        except (AttributeError, IndexError):
            return None

        local_index = soup.get_text(separator=' ', strip=True).find(XPOINTER_MARK)
        return item['start'] + local_index if local_index >= 0 else None

    def get_text_at_position(self, filename, xpointer, percentage):
        """Ebook text beginning at the reader's position, to look up in the transcript.

        KOReader's percentage follows its page layout, not the book's characters,
        so taking that fraction of the text can land several pages away. The
        xpointer is exact. The transcript match is timed from the start of this
        text, so it starts at the position instead of being centred on it.
        """
        try:
            book_path = self._resolve_book_path(filename)
            full_text, spine_map = self.extract_text_and_map(book_path)
            if not full_text: return None

            index = self._xpointer_to_index(spine_map, xpointer) if xpointer else None
            if index is None:
                logger.warning(f"  Could not resolve position '{xpointer}'; using {percentage:.2%} instead.")
                return self.get_text_at_percentage(filename, percentage)

            start = max(0, min(index, len(full_text) - POSITION_TEXT_LENGTH))
            return full_text[start:start + POSITION_TEXT_LENGTH]
        except FileNotFoundError:
            logger.error(f"  Book file not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"  Error extracting text from {filename}: {e}")
            return None

    def get_character_delta(self, filename, percentage_prev, percentage_new):
        try:
            book_path = self._resolve_book_path(filename)
        
            full_text, _ = self.extract_text_and_map(book_path)
            
            if not full_text: return None
            
            total_len = len(full_text)
            index_prev = int(total_len * percentage_prev)
            index_new = int(total_len * percentage_new)

            character_delta = abs(index_new - index_prev)
                        
            return character_delta
        except FileNotFoundError:
            logger.error(f"  Book file not found: {filename}")
            return None
        except Exception as e:
            logger.error(f"  Error calculating character delta for {filename}: {e}")
            return None
