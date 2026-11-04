import os
import time
import json
import schedule
import logging
import sys
from pathlib import Path
from rapidfuzz import process, fuzz

# Import local modules
from api_clients import ABSClient, KoSyncClient
from transcriber import AudioTranscriber
from ebook_utils import EbookParser

import logging
import os

# Add trace level logging
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
logging.TRACE = TRACE_LEVEL_NUM
def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.trace = trace

# Read user defined debug lecel, default to INFO. Check its an acual level other wise default INFO
env_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
try:
    log_level = getattr(logging, env_log_level)
except AttributeError:
    log_level = logging.INFO 

logging.basicConfig(
    level=log_level, 
    format='%(asctime)s %(levelname)s: %(message)s', 
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

DATA_DIR = Path("/data")
BOOKS_DIR = Path("/books")
DB_FILE = DATA_DIR / "mapping_db.json"
STATE_FILE = DATA_DIR / "last_state.json"

class SyncManager:
    def __init__(self):
        logger.info("Initializing Sync Manager...")
        self.abs_client = ABSClient()
        self.kosync_client = KoSyncClient()
        self.transcriber = AudioTranscriber(DATA_DIR)
        self.ebook_parser = EbookParser(BOOKS_DIR)
        self.db = self._load_db()
        self.state = self._load_state()
        
        # Load Sync Thresholds
        # ABS: Seconds (Default 60s)
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))
        # Kosync: Character (Default 400 Words) -> Converted to characters by multiplying by 5
        self.delta_kosync_char_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_WORDS", 400)) * 5


        logger.info(f"⚙️  Sync Thresholds: ABS={self.delta_abs_thresh}s, KoSync={self.delta_kosync_char_thresh} chars")
        
        self.startup_checks()
        self.cleanup_stale_jobs()

    def startup_checks(self):
        logger.info("--- Performing Connectivity Checks ---")
        abs_ok = self.abs_client.check_connection()
        kosync_ok = self.kosync_client.check_connection()
        if not abs_ok: logger.warning("⚠️  Audiobookshelf connection FAILED.")
        if not kosync_ok: logger.warning("⚠️  KoSync connection FAILED.")
        logger.info("----------------------------------------")

    def cleanup_stale_jobs(self):
        dirty = False
        for mapping in self.db['mappings']:
            if mapping.get('status') == 'processing':
                title = mapping.get('abs_title', 'Unknown')
                logger.warning(f"⚠️  CRASH DETECTED: Job for '{title}' was interrupted.")
                mapping['status'] = 'crashed'
                dirty = True
        if dirty: self._save_db()

    def _load_db(self):
        if DB_FILE.exists():
            with open(DB_FILE, 'r') as f: return json.load(f)
        return {"mappings": []}

    def _save_db(self):
        with open(DB_FILE, 'w') as f: json.dump(self.db, f, indent=4)

    def _load_state(self):
        if STATE_FILE.exists():
            with open(STATE_FILE, 'r') as f: return json.load(f)
        return {}

    def _save_state(self):
        with open(STATE_FILE, 'w') as f: json.dump(self.state, f, indent=4)

    def _get_abs_title(self, item):
        title = item.get('media', {}).get('metadata', {}).get('title')
        if not title: title = item.get('name')
        if not title: title = item.get('title')
        return title or "Unknown Title"

    def match_wizard(self):
        print("\n=== Matching Wizard (Queue Mode) ===")
        print("Fetching audiobooks from server...")
        audiobooks = self.abs_client.get_all_audiobooks()
        if not audiobooks:
            print("❌ No audiobooks found.")
            return

        ebooks = [f for f in BOOKS_DIR.glob("**/*.epub")] 
        if not ebooks:
            print("❌ No ebooks found in /books.")
            return

        search_term = input("\nFilter by title (Press Enter to view all): ").strip().lower()

        if search_term:
            # Filter Audiobooks based on title
            filtered_audiobooks = [
                ab for ab in audiobooks 
                if search_term in self._get_abs_title(ab).lower()
            ]
            # Filter Ebooks based on filename
            filtered_ebooks = [
                eb for eb in ebooks 
                if search_term in eb.name.lower()
            ]
        else:
            # If blank, keep lists as is
            filtered_audiobooks = audiobooks
            filtered_ebooks = ebooks
        
        if not filtered_audiobooks:
            print(f"❌ No audiobooks found matching term: '{search_term}'")
            return

        if not filtered_ebooks:
            print(f"❌ No ebooks found matching term: '{search_term}'")
            return
        
        print(f"\n--- Available Audiobooks ({len(audiobooks)} found) ---")
        for idx, ab in enumerate(filtered_audiobooks):
            title = self._get_abs_title(ab)
            print(f"{idx + 1}. {title} (ID: {ab.get('id')})")
        
        try:
            ab_choice = int(input("\nSelect Audiobook Number: ")) - 1
            selected_ab = filtered_audiobooks[ab_choice]
        except (ValueError, IndexError): return

        print("\n--- Available Ebooks ---")
        for idx, eb in enumerate(filtered_ebooks):
            print(f"{idx + 1}. {eb.name}")
        
        try:
            eb_choice = int(input("\nSelect Ebook Number: ")) - 1
            selected_eb = filtered_ebooks[eb_choice]
        except (ValueError, IndexError): return

        kosync_doc_id = self.ebook_parser.get_kosync_id(selected_eb)
        final_title = self._get_abs_title(selected_ab)

        print(f"\nQueuing '{final_title}' <-> '{selected_eb.name}'")
        
        mapping = {
            "abs_id": selected_ab['id'],
            "abs_title": final_title,
            "ebook_filename": selected_eb.name,
            "kosync_doc_id": kosync_doc_id,
            "transcript_file": None,
            "status": "pending"
        }

        self.db['mappings'] = [m for m in self.db['mappings'] if m['abs_id'] != selected_ab['id']]
        self.db['mappings'].append(mapping)
        self._save_db()
        print("✅ Job Queued Successfully!")

    def check_pending_jobs(self):
        self.db = self._load_db()
        for mapping in self.db['mappings']:
            if mapping.get('status') == 'pending':
                abs_title = mapping.get('abs_title', 'Unknown')
                logger.info(f"🚀 Found pending job for: {abs_title}")
                
                mapping['status'] = 'processing'
                self._save_db()
                
                try:
                    audio_files = self.abs_client.get_audio_files(mapping['abs_id'])
                    if not audio_files:
                        logger.error(f"❌ No audio files found for {abs_title}.")
                        mapping['status'] = 'failed'
                        self._save_db()
                        continue

                    logger.info("   Starting transcription...")
                    transcript_path = self.transcriber.process_audio(mapping['abs_id'], audio_files)
                    
                    logger.info("   Priming ebook cache...")
                    self.ebook_parser.extract_text_and_map(mapping['ebook_filename'])

                    mapping['transcript_file'] = str(transcript_path)
                    mapping['status'] = 'active'
                    self._save_db()
                    logger.info(f"✅ Job complete! {abs_title} is now active and syncing.")

                except Exception as e:
                    logger.error(f"❌ Job failed for {abs_title}: {e}")
                    mapping['status'] = 'failed_retry_later' 
                    self._save_db()

    def sync_cycle(self):
        logger.debug("Starting Sync Cycle...")
        self.db = self._load_db() 
        
        if not self.db['mappings']: return

        for mapping in self.db['mappings']:
            if mapping.get('status', 'active') != 'active': continue
                
            abs_id = mapping['abs_id']
            kosync_id = mapping['kosync_doc_id']
            transcript_path = mapping['transcript_file']
            ebook_filename = mapping['ebook_filename']
            abs_title = mapping.get('abs_title', 'Unknown')

            try:
                abs_progress = self.abs_client.get_progress(abs_id)
                kosync_progress, kosync_xpointer = self.kosync_client.get_position(kosync_id)
            except Exception as e:
                logger.error(f"Fetch failed for {abs_title}: {e}")
                continue

            kosync_char = self.ebook_parser.get_position_index(ebook_filename, kosync_xpointer, kosync_progress)
            if kosync_char is None:
                logger.error(f"Could not read a position in {ebook_filename}; skipping {abs_title}")
                continue

            ## Define and set defaults if empty.
            defaults = {"abs_ts": 0, "kosync_pct": 0, "last_updated": 0, "kosync_char": 0}
            existing_data = self.state.get(abs_id, {})
            prev_state = defaults | existing_data

            # State saved before kosync_char existed has nothing to compare with.
            # Start from here rather than treat the whole position as a move.
            if existing_data and 'kosync_char' not in existing_data:
                prev_state['kosync_char'] = kosync_char
                self.state[abs_id] = prev_state
                self._save_state()

            abs_delta = abs(abs_progress - prev_state['abs_ts'])
            # Measured in characters from the exact position. KOReader's percentage
            # follows its page layout, so it differs from the percentage written
            # back by this bridge even when the reader hasn't moved.
            kosync_delta = abs(kosync_char - prev_state['kosync_char'])

            # --- THRESHOLD LOGIC ---
            # A move below the threshold leaves state alone, so small moves add up
            # until they cross it instead of being lost one at a time.
            abs_changed = abs_delta > self.delta_abs_thresh
            kosync_changed = kosync_delta > self.delta_kosync_char_thresh

            if abs_delta > 0 and not abs_changed:
                logger.debug(f"  ✋ ABS delta {abs_delta:.2f}s (Below threshold {self.delta_abs_thresh}s): {abs_title}")
            if kosync_delta > 0 and not kosync_changed:
                logger.debug(f"  ✋ KoSync delta {kosync_delta} chars (Below threshold {self.delta_kosync_char_thresh}): {ebook_filename}")

            if not abs_changed and not kosync_changed: continue

            logger.info(f"Change detected for '{abs_title}'")
            logger.info(f"  📊 ABS: {prev_state['abs_ts']:.2f}s -> {abs_progress:.2f}s")
            logger.info(f"  📊 KoSync: {prev_state['kosync_pct']:.2%} -> {kosync_progress:.2%} ({kosync_delta} chars)")
            
            source = "ABS" if abs_changed else "KOSYNC"
            if abs_changed and kosync_changed:
                logger.warning(f"  ⚠️ Conflict! Defaulting to ABS.")
                source = "ABS"

            updated_ok = False
            # Set when this change could not be synced: no text at the position,
            # no match on the other side, or the server rejected the write. State
            # is then left untouched so the next cycle tries again. Recording it
            # as synced would drop the position for good unless it moved again.
            retry_reason = None
            try:
                if source == "ABS":
                    target_text = self.transcriber.get_text_at_time(transcript_path, abs_progress)
                    if target_text:
                        logger.info(f"  🔍 Searching Ebook for text: '{target_text[:60]}...'")
                        logger.debug(f"  🔍 Searching Ebook for text: '{target_text}'")
                        matched_pct, xpath, matched_index = self.ebook_parser.find_text_location(ebook_filename, target_text)
                        if matched_pct is not None:
                            logger.info(f"  ✅ Match at {matched_pct:.2%}. Sending Update...")

                            if self.kosync_client.update_progress(kosync_id, matched_pct, xpath):
                                prev_state['abs_ts'] = abs_progress
                                prev_state['kosync_pct'] = matched_pct
                                # Where the next cycle will read this write back from. The
                                # xpath only points at an element, which can start well
                                # before the matched text; a baseline of matched_index
                                # would read that gap as the reader moving.
                                written_char = self.ebook_parser.get_position_index(ebook_filename, xpath, matched_pct)
                                prev_state['kosync_char'] = matched_index if written_char is None else written_char
                                updated_ok = True
                            else:
                                retry_reason = "KoSync write failed"
                        else:
                            logger.error("  ❌ Ebook text match FAILED.")
                            retry_reason = "Ebook text match failed"
                    else:
                        retry_reason = "No transcript text at this time"
                else:
                    target_text = self.ebook_parser.get_text_at_position(ebook_filename, kosync_xpointer, kosync_progress)
                    if target_text:
                        logger.info(f"   🔍 Searching Transcript for text: '{target_text[:60]}...'")
                        matched_time = self.transcriber.find_time_for_text(transcript_path, target_text)
                        if matched_time is not None:
                            logger.info(f"  ✅ Match at {matched_time:.2f}s. Sending Update...")
                            if self.abs_client.update_progress(abs_id, matched_time):
                                prev_state['abs_ts'] = matched_time
                                prev_state['kosync_pct'] = kosync_progress
                                prev_state['kosync_char'] = kosync_char
                                updated_ok = True
                            else:
                                retry_reason = "ABS write failed"
                        else:
                            logger.error("  ❌ Transcript text match FAILED.")
                            retry_reason = "Transcript text match failed"
                    else:
                        retry_reason = "No ebook text at this position"

                if updated_ok:
                    prev_state['last_updated'] = time.time()
                    self.state[abs_id] = prev_state
                    self._save_state()
                    logger.info("  💾 State saved.")
                else:
                    logger.warning(f"  ⏭️  {retry_reason}, state not saved. Will retry next cycle.")
            except Exception as e:
                logger.error(f"   Error syncing {abs_title}: {e}")

    def run_daemon(self):
        period = int(os.getenv("SYNC_PERIOD_MINS", 5))
        schedule.every(period).minutes.do(self.sync_cycle)
        schedule.every(1).minutes.do(self.check_pending_jobs)
        
        logger.info(f"Daemon running. Sync every {period} mins. Checking queue every 1 min.")
        self.check_pending_jobs()
        
        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    manager = SyncManager()
    if len(sys.argv) > 1 and sys.argv[1] == "match":
        manager.match_wizard()
    else:
        manager.run_daemon()
