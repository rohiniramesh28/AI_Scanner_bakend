import os
import uuid
import shutil
import asyncio
from threading import Lock
from PIL import Image

from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse

from image_search import search_book, add_book
from firebase_service import save_book_for_user, user_has_book, verify_user

# -------- FLOW 2 IMPORTS --------
from vision_ai.vision import detect_book
from vision_ai.book_fetcher import get_book_info
from vision_ai.ai_summary import summarize_book


app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_FILE_SIZE = 4 * 1024 * 1024
index_lock = Lock()


# =========================================================
# 🔐 AUTH HELPER (CRITICAL FIX)
# =========================================================
async def get_uid(request: Request):
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ")[1]
    return verify_user(token)


# =========================================================
# 🌍 HEALTH CHECK
# =========================================================
@app.get("/")
def root():
    return {"status": "running"}


# =========================================================
# 🖼 IMAGE VALIDATION
# =========================================================
def validate_image(path: str):
    try:
        with Image.open(path) as img:
            img.load()
        return True
    except:
        return False


# =========================================================
# 💾 SAVE TEMP FILE
# =========================================================
def save_temp(file: UploadFile):
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)

    if size > MAX_FILE_SIZE:
        raise ValueError("file_too_large")

    path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex}.jpg")

    with open(path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    file.file.close()
    return path


# =========================================================
# 🔍 FLOW-1 → SCAN BOOK
# =========================================================
@app.post("/scan")
async def scan(request: Request, file: UploadFile = File(...)):

    uid = await get_uid(request)
    if not uid:
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    path = save_temp(file)

    if not validate_image(path):
        os.remove(path)
        return JSONResponse(status_code=400, content={"status": "invalid_image"})

    try:
        book, score = await asyncio.to_thread(search_book, path)

        if book is None:
            return {"status": "not_found"}

        title = book["title"]

        if user_has_book(uid, title):
            return {"status": "owned", "title": title}

        return {
            "status": "found",
            "title": title,
            "confidence": round(float(score), 3)
        }

    finally:
        if os.path.exists(path):
            os.remove(path)


# =========================================================
# 📷 ADD BOOK (LEARN)
# =========================================================
@app.post("/add")
async def add(request: Request, file: UploadFile = File(...)):

    uid = await get_uid(request)
    if not uid:
        return JSONResponse(status_code=401, content={"status": "unauthorized"})

    path = None

    try:
        path = save_temp(file)
    except ValueError:
        return JSONResponse(status_code=413, content={"status": "file_too_large"})

    if not validate_image(path):
        if os.path.exists(path):
            os.remove(path)
        return JSONResponse(status_code=400, content={"status": "invalid_image"})

    try:
        # check already exists
        book, score = await asyncio.to_thread(search_book, path)

        # ---------- EXISTING ----------
        if book is not None:
            title = book["title"]

            if user_has_book(uid, title):
                return {"status": "already_saved", "title": title}

            save_book_for_user(uid, title)
            return {"status": "saved_existing", "title": title}

        # ---------- NEW ----------
        unique_title = f"Book_{uuid.uuid4().hex[:8]}"

        with index_lock:
            await asyncio.to_thread(add_book, path, unique_title)

        save_book_for_user(uid, unique_title)

        return {"status": "saved_new", "title": unique_title}

    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

    finally:
        if path and os.path.exists(path):
            os.remove(path)


# =========================================================
# 🤖 FLOW-2 → AI BOOK EXPLAIN
# =========================================================
@app.post("/ask-book-ai")
async def ask_book_ai(file: UploadFile = File(...)):

    path = f"{UPLOAD_DIR}/{uuid.uuid4().hex}.jpg"

    try:
        contents = await file.read()

        if not contents:
            return JSONResponse(status_code=400, content={"error": "Empty image uploaded"})

        with open(path, "wb") as f:
            f.write(contents)

        book_name = await asyncio.to_thread(detect_book, path)

        if not book_name:
            return JSONResponse(status_code=422, content={"error": "Could not identify book"})

        book = get_book_info(book_name)

        if not book:
            return JSONResponse(status_code=404, content={"error": f"No info found for '{book_name}'"})

        overview = await asyncio.to_thread(summarize_book, book)

        return {
            "title": book["title"],
            "overview": overview
        }

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    finally:
        if os.path.exists(path):
            os.remove(path)