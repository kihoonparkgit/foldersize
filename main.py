import sys
import os

# Add the parent directory to sys.path so we can import modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gui.app import FolderSizeApp

if __name__ == "__main__":
    app = FolderSizeApp()
    app.mainloop()
