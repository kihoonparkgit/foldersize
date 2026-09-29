import os
from collections import defaultdict

class DirectoryScanner:
    def __init__(self, callback=None):
        self.callback = callback
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def scan(self, root_dir):
        """
        Scans a directory recursively and returns a dictionary 
        mapping folder paths to their total size in bytes.
        """
        folder_sizes = defaultdict(int)
        
        def _scan_recursive(current_path):
            if self.is_cancelled:
                return 0
            
            total_size = 0
            try:
                with os.scandir(current_path) as it:
                    for entry in it:
                        if self.is_cancelled:
                            return 0
                        
                        if entry.is_file(follow_symlinks=False):
                            total_size += entry.stat(follow_symlinks=False).st_size
                        elif entry.is_dir(follow_symlinks=False):
                            if self.callback:
                                self.callback(entry.path)
                            total_size += _scan_recursive(entry.path)
            except (PermissionError, FileNotFoundError, OSError):
                # Ignore directories we can't access
                pass

            folder_sizes[current_path] = total_size
            return total_size

        if self.callback:
            self.callback(root_dir)
            
        _scan_recursive(root_dir)
        return folder_sizes
