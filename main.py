from __future__ import annotations

from datetime import datetime, timezone
from os import getenv
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport import requests
from google.oauth2 import id_token
from pymongo import MongoClient

app = FastAPI()

firebase_request_adapter = requests.Request()

app.mount('/static', StaticFiles(directory='static'), name='static')
templates = Jinja2Templates(directory='templates')


def _load_env_file() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and getenv(key) is None:
            # Keep environment variables provided by the shell as source of truth.
            from os import environ

            environ[key] = value


def _parse_cookie_token(cookie_header: str | None) -> str:
    if not cookie_header:
        return ""
    for cookie_item in cookie_header.split(";"):
        token_pair = cookie_item.strip().split("=", 1)
        if len(token_pair) == 2 and token_pair[0] == "token":
            return token_pair[1]
    return ""


def _verify_firebase_user(request: Request) -> dict[str, str] | None:
    cookie_header = request.headers.get("cookie")
    token = _parse_cookie_token(cookie_header)
    if not token:
        return None

    try:
        decoded_token = id_token.verify_firebase_token(token, firebase_request_adapter)
    except ValueError:
        return None
        
    firebase_uid = (
        decoded_token.get("user_id")
        or decoded_token.get("sub")
        or decoded_token.get("uid")
    )
    email = decoded_token.get("email")
    if not firebase_uid:
        return None

    return {"uid": firebase_uid, "email": email or ""}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_load_env_file()
mongo_uri = getenv("MONGO_URI", "").strip()
if not mongo_uri:
    raise RuntimeError("MONGO_URI is not set. Add it to your environment or .env file.")

mongo_client = MongoClient(mongo_uri)
database = mongo_client["A2-3183338"]
users_collection = database["users"]
tweets_collection = database["tweets"]


def _get_or_create_user(firebase_uid: str, email: str) -> dict:
    existing_user = users_collection.find_one({"firebase_uid": firebase_uid})
    if existing_user:
        if email and existing_user.get("email") != email:
            users_collection.update_one(
                {"_id": existing_user["_id"]},
                {"$set": {"email": email}},
            )
            existing_user["email"] = email
        return existing_user

    new_user = {
        "firebase_uid": firebase_uid,
        "email": email,
        "username": "",
        "created_at": _utc_now(),
    }
    inserted = users_collection.insert_one(new_user)
    new_user["_id"] = inserted.inserted_id
    return new_user


@app.get("/")
async def root(request: Request):
    token = _parse_cookie_token(request.headers.get("cookie"))
    firebase_user = _verify_firebase_user(request)
    context = {
        "request": request,
        "is_authenticated": False,
        "needs_username": False,
        "username": "",
        "tweets": [],
        "error_message": "",
    }

    if firebase_user is None:
        response = templates.TemplateResponse("main.html", context)
        if token:
            response.delete_cookie("token", path="/", samesite="strict")
        return response

    local_user = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    needs_username = not bool(local_user.get("username"))

    tweet_documents = list(
        tweets_collection.find({"user_id": local_user["_id"]}).sort("created_at", -1)
    )
    for tweet in tweet_documents:
        tweet["_id"] = str(tweet["_id"])
        tweet["created_at"] = tweet["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")

    context.update(
        {
            "is_authenticated": True,
            "needs_username": needs_username,
            "username": local_user.get("username", ""),
            "tweets": tweet_documents,
        }
    )
    return templates.TemplateResponse("main.html", context)


@app.post("/set-username")
async def set_username(request: Request, username: str = Form(...)):
    firebase_user = _verify_firebase_user(request)
    if firebase_user is None:
        return RedirectResponse(url="/", status_code=303)

    cleaned_username = username.strip()
    if not cleaned_username:
        return templates.TemplateResponse(
            "main.html",
            {
                "request": request,
                "is_authenticated": True,
                "needs_username": True,
                "username": "",
                "tweets": [],
                "error_message": "Username cannot be empty.",
            },
            status_code=400,
        )

    user_document = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    if user_document.get("username"):
        return RedirectResponse(url="/", status_code=303)

    duplicate_user = users_collection.find_one({"username": cleaned_username})
    if duplicate_user is not None:
        return templates.TemplateResponse(
            "main.html",
            {
                "request": request,
                "is_authenticated": True,
                "needs_username": True,
                "username": "",
                "tweets": [],
                "error_message": "That username is taken. Please choose another one.",
            },
            status_code=409,
        )

    users_collection.update_one(
        {"_id": user_document["_id"]},
        {"$set": {"username": cleaned_username}},
    )
    return RedirectResponse(url="/", status_code=303)


@app.post("/tweets")
async def create_tweet(request: Request, content: str = Form(...)):
    firebase_user = _verify_firebase_user(request)
    if firebase_user is None:
        return RedirectResponse(url="/", status_code=303)

    user_document = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    if not user_document.get("username"):
        return RedirectResponse(url="/", status_code=303)

    cleaned_content = content.strip()
    if not cleaned_content or len(cleaned_content) > 280:
        tweet_documents = list(
            tweets_collection.find({"user_id": user_document["_id"]}).sort("created_at", -1)
        )
        for tweet in tweet_documents:
            tweet["_id"] = str(tweet["_id"])
            tweet["created_at"] = tweet["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")

        return templates.TemplateResponse(
            "main.html",
            {
                "request": request,
                "is_authenticated": True,
                "needs_username": False,
                "username": user_document["username"],
                "tweets": tweet_documents,
                "error_message": "Tweet must be between 1 and 280 characters.",
            },
            status_code=400,
        )

    tweet_document = {
        "user_id": user_document["_id"],
        "content": cleaned_content,
        "created_at": _utc_now(),
    }
    tweets_collection.insert_one(tweet_document)
    return RedirectResponse(url="/", status_code=303)