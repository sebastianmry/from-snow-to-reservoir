"""List all remaining TIF dates per site from Google Drive."""
import re
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive

FOLDER_ID = "1EdYn2RbULuEYj8dnPbK9Zshia6G50ssE"

gauth = GoogleAuth()
gauth.LoadCredentialsFile("gdrive_credentials.json")
if gauth.credentials is None:
    gauth.LocalWebserverAuth()
elif gauth.access_token_expired:
    gauth.Refresh()
else:
    gauth.Authorize()
gauth.SaveCredentialsFile("gdrive_credentials.json")
drive = GoogleDrive(gauth)

def get_subfolders(folder_id):
    return drive.ListFile({"q": f"'{folder_id}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'"}).GetList()

def get_tifs(folder_id):
    return drive.ListFile({"q": f"'{folder_id}' in parents and trashed=false and title contains '.tif'"}).GetList()

for folder in get_subfolders(FOLDER_ID):
    files = get_tifs(folder["id"])
    dates = sorted([re.search(r"_(\d{8})_", f["title"]).group(1) for f in files if re.search(r"_(\d{8})_", f["title"])])
    print(f"\n{folder['title'].upper()} ({len(dates)} files):")
    print(", ".join(dates))
