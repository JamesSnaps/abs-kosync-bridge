import os
import requests
import logging
import time
import hashlib

logger = logging.getLogger(__name__)

class ABSClient:
    def __init__(self):
        self.base_url = os.environ.get("ABS_SERVER", "").rstrip('/')
        self.token = os.environ.get("ABS_KEY")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def check_connection(self):
        url = f"{self.base_url}/api/me"
        try:
            r = requests.get(url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                logger.info(f"✅ Connected to Audiobookshelf as user: {r.json().get('username', 'Unknown')}")
                return True
            else:
                logger.error(f"❌ Audiobookshelf Connection Failed: {r.status_code} - {r.text}")
                return False
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ Could not connect to Audiobookshelf at {self.base_url}. Check URL and Docker Network.")
            return False
        except Exception as e:
            logger.error(f"❌ Audiobookshelf Error: {e}")
            return False

    def get_all_audiobooks(self):
        lib_url = f"{self.base_url}/api/libraries"
        try:
            r = requests.get(lib_url, headers=self.headers)
            if r.status_code != 200:
                logger.error(f"Failed to fetch libraries: {r.status_code} - {r.text}")
                return []
            
            libraries = r.json().get('libraries', [])
            all_audiobooks = []

            for lib in libraries:
                logger.info(f"Scanning library: {lib['name']}...")
                lib_id = lib['id']
                items_url = f"{self.base_url}/api/libraries/{lib_id}/items"
                params = {"mediaType": "audiobook"}
                r_items = requests.get(items_url, headers=self.headers, params=params)
                if r_items.status_code == 200:
                    results = r_items.json().get('results', [])
                    all_audiobooks.extend(results)
                else:
                    logger.warning(f"Could not fetch items for library {lib['name']}")

            logger.info(f"Found {len(all_audiobooks)} audiobooks across {len(libraries)} libraries.")
            return all_audiobooks

        except Exception as e:
            logger.error(f"Exception fetching audiobooks: {e}")
            return []

    def get_audio_files(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                data = r.json()
                files = []
                audio_files = data.get('media', {}).get('audioFiles', [])
                for af in audio_files:
                    stream_url = f"{self.base_url}/api/items/{item_id}/file/{af['ino']}"
                    stream_url += f"?token={self.token}" 
                    
                    extension = af.get('metadata', {}).get('ext') or 'mp3'
                    if not extension.startswith('.'):
                        extension = f".{extension}"
                    
                    files.append({
                        "stream_url": stream_url,
                        "ext": extension
                     })
                return files
            else:
                logger.error(f"Failed to get audio files for {item_id}: {r.status_code} - {r.text}")
                return []
        except Exception as e:
            logger.error(f"Error getting audio files: {e}")
            return []

    def get_progress(self, item_id):
        # A fetch FAILURE must not look like "position zero" -- the caller would
        # treat that as a huge backwards jump and rewind the ebook to page one.
        # A 404 genuinely means "no progress recorded yet" and is safe as 0.0.
        url = f"{self.base_url}/api/me/progress/{item_id}"
        r = requests.get(url, headers=self.headers, timeout=15)
        if r.status_code == 404:
            return 0.0
        r.raise_for_status()
        return float(r.json().get('currentTime', 0) or 0)

    def update_progress(self, item_id, timestamp):
        # Read-modify-write. Blindly sending duration=0/isFinished=False wipes
        # the real duration and un-finishes completed books.
        url = f"{self.base_url}/api/me/progress/{item_id}"

        existing = {}
        try:
            r = requests.get(url, headers=self.headers, timeout=15)
            if r.status_code == 200:
                existing = r.json()
        except Exception as e:
            logger.warning(f"  Could not read existing ABS progress, sending minimal update: {e}")

        payload = {"currentTime": timestamp}

        duration = existing.get('duration') or 0
        if duration <= 0:
            # An existing record can carry duration=0 (earlier versions of this
            # client wrote that unconditionally). Without a duration we cannot
            # send `progress`, and ABS then shows a stale percentage that
            # disagrees with its own remaining-time figure. Fall back to the
            # item's real duration.
            try:
                item = requests.get(f"{self.base_url}/api/items/{item_id}",
                                    headers=self.headers, timeout=15)
                if item.status_code == 200:
                    duration = item.json().get('media', {}).get('duration') or 0
            except Exception as e:
                logger.warning(f"  Could not read item duration: {e}")

        if duration > 0:
            payload['duration'] = duration
            payload['progress'] = min(max(timestamp / duration, 0.0), 1.0)

        if 'isFinished' in existing:
            payload['isFinished'] = existing['isFinished']

        try:
            r = requests.patch(url, headers=self.headers, json=payload, timeout=15)
            if r.status_code not in (200, 201):
                logger.error(f"  ABS update failed: {r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"  Failed to update ABS progress: {e}")

class KoSyncClient:
    def __init__(self):
        self.base_url = os.environ.get("KOSYNC_SERVER", "").rstrip('/')
        self.user = os.environ.get("KOSYNC_USER")
        self.auth_token = hashlib.md5(os.environ.get("KOSYNC_KEY", "").encode('utf-8')).hexdigest()

    def check_connection(self):
        url = f"{self.base_url}/healthcheck"
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, "accept": "application/json"}
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                 logger.info(f"✅ Connected to KoSync Server at {self.base_url}")
                 return True
            
            url_sync = f"{self.base_url}/syncs/progress/test-connection"
            r = requests.get(url_sync, headers=headers, timeout=5)
            logger.info(f"✅ Connected to KoSync Server (Response: {r.status_code})")
            return True
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ Could not connect to KoSync at {self.base_url}. Check URL.")
            return False
        except Exception as e:
            logger.error(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        # As with ABS: only a genuine "nothing recorded" answer may return 0.0.
        # Network errors and auth failures must raise so the sync cycle skips
        # this book rather than rewinding it to the start.
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, 'accept': 'application/vnd.koreader.v1+json'}
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 404:
            return 0.0
        r.raise_for_status()
        data = r.json() or {}
        # A synced-but-unread document can come back with an empty body.
        return float(data.get('percentage', 0) or 0)

    def update_progress(self, doc_id, percentage, xpath=None):
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, 'accept': 'application/vnd.koreader.v1+json', 'content-type': 'application/json'}
        url = f"{self.base_url}/syncs/progress"
        
        # Use XPath if generated, otherwise fallback to percentage string
        progress_val = xpath if xpath else f"{percentage:.2%}"
        
        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": progress_val, 
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot", 
            "timestamp": int(time.time())
        }
        
        try:
            # Reverted to simple PUT logic
            r = requests.put(url, headers=headers, json=payload)
            
            if r.status_code not in [200, 201]:
                logger.error(f"  KoSync Update Failed: {r.status_code} - {r.text}")
            else:
                logger.info(f"  KoSync updated successfully (HTTP {r.status_code})")
                
        except Exception as e:
            logger.error(f"Failed to update KoSync: {e}")
