from __future__ import annotations

from datetime import datetime, timezone
from os import getenv
from pathlib import Path
import re
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from azure.storage.blob import BlobServiceClient, ContentSettings
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


def _format_tweets_for_view(tweet_documents: list[dict]) -> list[dict]:
    for tweet in tweet_documents:
        tweet["_id"] = str(tweet["_id"])
        tweet["created_at"] = tweet["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC")
    return tweet_documents


def _build_authenticated_context(
    request: Request,
    user_document: dict,
    error_message: str = "",
    search_query: str = "",
    user_results: list[dict] | None = None,
    tweet_results: list[dict] | None = None,
) -> dict:
    user_id = user_document.get("_id")
    tweet_documents: list[dict] = []
    if user_id is not None:
        tweet_documents = list(
            tweets_collection.find({"user_id": user_id}).sort("created_at", -1)
        )
    return {
        "request": request,
        "is_authenticated": True,
        "needs_username": not bool(user_document.get("username")),
        "username": user_document.get("username", ""),
        "profile_image_url": _resolve_profile_image_url(user_document),
        "tweets": _format_tweets_for_view(tweet_documents),
        "error_message": error_message,
        "search_query": search_query,
        "user_results": user_results or [],
        "tweet_results": tweet_results or [],
    }


_load_env_file()
mongo_uri = getenv("MONGO_URI", "").strip()
if not mongo_uri:
    raise RuntimeError("MONGO_URI is not set. Add it to your environment or .env file.")

mongo_client = MongoClient(mongo_uri)
database = mongo_client["A2-3183338"]
users_collection = database["users"]
tweets_collection = database["tweets"]
follows_collection = database["follows"]

blob_connection_string = getenv("AZURITE_CONNECTION_STRING", "UseDevelopmentStorage=true")
blob_container_name = getenv("AZURITE_CONTAINER_NAME", "profile-images")
blob_api_version = getenv("AZURITE_BLOB_API_VERSION", "2021-12-02")
blob_service_client = BlobServiceClient.from_connection_string(
    blob_connection_string,
    api_version=blob_api_version,
)
blob_container_client = blob_service_client.get_container_client(blob_container_name)


def _ensure_blob_container() -> None:
    try:
        blob_container_client.create_container()
    except Exception:
        # The container already exists in normal operation.
        pass


def _detect_image_extension(file_bytes: bytes) -> tuple[str, str] | None:
    if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if file_bytes.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    return None


def _resolve_profile_image_url(user_document: dict) -> str:
    username = user_document.get("username", "")
    blob_name = user_document.get("profile_image_blob_name", "")
    if username and blob_name:
        return f"/profile/{username}/photo-file"
    return user_document.get("profile_image_url", "")


_ensure_blob_container()


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


def _get_authenticated_user_or_redirect(request: Request) -> tuple[dict | None, RedirectResponse | None]:
    firebase_user = _verify_firebase_user(request)
    if firebase_user is None:
        return None, RedirectResponse(url="/", status_code=303)

    user_document = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    if not user_document.get("username"):
        return None, RedirectResponse(url="/", status_code=303)
    return user_document, None


def _build_profile_context(
    request: Request,
    viewer_document: dict,
    profile_user: dict,
    error_message: str = "",
) -> dict:
    last_ten_tweets = list(
        tweets_collection.find({"user_id": profile_user["_id"]}).sort("created_at", -1).limit(10)
    )
    follower_count = follows_collection.count_documents({"following_user_id": profile_user["_id"]})
    following_count = follows_collection.count_documents({"follower_user_id": profile_user["_id"]})

    is_own_profile = viewer_document["_id"] == profile_user["_id"]
    is_following = False
    if not is_own_profile:
        is_following = (
            follows_collection.find_one(
                {
                    "follower_user_id": viewer_document["_id"],
                    "following_user_id": profile_user["_id"],
                }
            )
            is not None
        )

    return {
        "request": request,
        "viewer_username": viewer_document["username"],
        "viewer_profile_image_url": _resolve_profile_image_url(viewer_document),
        "profile_found": True,
        "error_message": error_message,
        "profile_username": profile_user["username"],
        "profile_email": profile_user.get("email", ""),
        "profile_image_url": _resolve_profile_image_url(profile_user),
        "profile_created_at": profile_user["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
        "is_own_profile": is_own_profile,
        "is_following": is_following,
        "follower_count": follower_count,
        "following_count": following_count,
        "tweets": _format_tweets_for_view(last_ten_tweets),
    }


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
        "search_query": "",
        "user_results": [],
        "tweet_results": [],
    }

    if firebase_user is None:
        response = templates.TemplateResponse("main.html", context)
        if token:
            response.delete_cookie("token", path="/", samesite="strict")
        return response

    local_user = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    context.update(_build_authenticated_context(request, local_user))
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
            _build_authenticated_context(
                request=request,
                user_document={"username": ""},
                error_message="Username cannot be empty.",
            ),
            status_code=400,
        )

    user_document = _get_or_create_user(firebase_user["uid"], firebase_user["email"])
    if user_document.get("username"):
        return RedirectResponse(url="/", status_code=303)

    duplicate_user = users_collection.find_one({"username": cleaned_username})
    if duplicate_user is not None:
        return templates.TemplateResponse(
            "main.html",
            _build_authenticated_context(
                request=request,
                user_document={"username": ""},
                error_message="That username is taken. Please choose another one.",
            ),
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
        return templates.TemplateResponse(
            "main.html",
            _build_authenticated_context(
                request=request,
                user_document=user_document,
                error_message="Tweet must be between 1 and 280 characters.",
            ),
            status_code=400,
        )

    tweet_document = {
        "user_id": user_document["_id"],
        "content": cleaned_content,
        "created_at": _utc_now(),
    }
    tweets_collection.insert_one(tweet_document)
    return RedirectResponse(url="/", status_code=303)


@app.get("/search")
async def search(request: Request, q: str = ""):
    user_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    cleaned_query = q.strip()
    if not cleaned_query:
        return RedirectResponse(url="/", status_code=303)

    prefix_regex = re.compile(f"^{re.escape(cleaned_query)}", re.IGNORECASE)

    matched_users = list(
        users_collection.find(
            {
                "$and": [
                    {"username": {"$regex": prefix_regex}},
                    {"username": {"$ne": ""}},
                ]
            },
            {"username": 1},
        )
        .sort("username", 1)
        .limit(25)
    )
    user_results = [{"username": user_row.get("username", "")} for user_row in matched_users]

    matched_tweets = list(
        tweets_collection.find({"content": {"$regex": prefix_regex}})
        .sort("created_at", -1)
        .limit(25)
    )
    user_ids = [tweet["user_id"] for tweet in matched_tweets]
    authors = {
        user_row["_id"]: user_row.get("username", "")
        for user_row in users_collection.find({"_id": {"$in": user_ids}}, {"username": 1})
    }
    tweet_results = []
    for tweet in matched_tweets:
        tweet_results.append(
            {
                "content": tweet.get("content", ""),
                "created_at": tweet["created_at"].strftime("%Y-%m-%d %H:%M:%S UTC"),
                "username": authors.get(tweet["user_id"], "unknown"),
            }
        )

    return templates.TemplateResponse(
        "main.html",
        _build_authenticated_context(
            request=request,
            user_document=user_document,
            search_query=cleaned_query,
            user_results=user_results,
            tweet_results=tweet_results,
        ),
    )


@app.get("/profile/{username}")
async def profile(request: Request, username: str):
    viewer_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    target_username = username.strip()
    if not target_username:
        return RedirectResponse(url="/", status_code=303)

    profile_user = users_collection.find_one({"username": target_username})
    if profile_user is None:
        return templates.TemplateResponse(
            "profile.html",
            {
                "request": request,
                "viewer_username": viewer_document["username"],
                "viewer_profile_image_url": _resolve_profile_image_url(viewer_document),
                "profile_found": False,
                "error_message": "User profile not found.",
            },
            status_code=404,
        )

    return templates.TemplateResponse("profile.html", _build_profile_context(request, viewer_document, profile_user))


@app.post("/profile/{username}/follow")
async def follow_user(request: Request, username: str):
    viewer_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    profile_user = users_collection.find_one({"username": username.strip()})
    if profile_user is None or viewer_document["_id"] == profile_user["_id"]:
        return RedirectResponse(url=f"/profile/{username}", status_code=303)

    existing_follow = follows_collection.find_one(
        {
            "follower_user_id": viewer_document["_id"],
            "following_user_id": profile_user["_id"],
        }
    )
    if existing_follow is None:
        follows_collection.insert_one(
            {
                "follower_user_id": viewer_document["_id"],
                "following_user_id": profile_user["_id"],
                "created_at": _utc_now(),
            }
        )
    return RedirectResponse(url=f"/profile/{profile_user['username']}", status_code=303)


@app.post("/profile/{username}/unfollow")
async def unfollow_user(request: Request, username: str):
    viewer_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    profile_user = users_collection.find_one({"username": username.strip()})
    if profile_user is None or viewer_document["_id"] == profile_user["_id"]:
        return RedirectResponse(url=f"/profile/{username}", status_code=303)

    follows_collection.delete_one(
        {
            "follower_user_id": viewer_document["_id"],
            "following_user_id": profile_user["_id"],
        }
    )
    return RedirectResponse(url=f"/profile/{profile_user['username']}", status_code=303)


@app.post("/profile/{username}/photo")
async def upload_profile_photo(request: Request, username: str, photo: UploadFile = File(...)):
    viewer_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    profile_user = users_collection.find_one({"username": username.strip()})
    if profile_user is None:
        return RedirectResponse(url="/", status_code=303)
    if viewer_document["_id"] != profile_user["_id"]:
        return RedirectResponse(url=f"/profile/{profile_user['username']}", status_code=303)

    file_bytes = await photo.read()
    if not file_bytes:
        return templates.TemplateResponse(
            "profile.html",
            _build_profile_context(
                request=request,
                viewer_document=viewer_document,
                profile_user=profile_user,
                error_message="Please select an image to upload.",
            ),
            status_code=400,
        )

    detected_type = _detect_image_extension(file_bytes)
    if detected_type is None:
        return templates.TemplateResponse(
            "profile.html",
            _build_profile_context(
                request=request,
                viewer_document=viewer_document,
                profile_user=profile_user,
                error_message="Profile picture must be a JPG or PNG file.",
            ),
            status_code=400,
        )

    extension, mime_type = detected_type
    blob_name = f"{profile_user['username']}/{uuid4().hex}.{extension}"
    blob_client = blob_container_client.get_blob_client(blob_name)
    try:
        blob_client.upload_blob(
            file_bytes,
            overwrite=True,
            content_settings=ContentSettings(content_type=mime_type),
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "profile.html",
            _build_profile_context(
                request=request,
                viewer_document=viewer_document,
                profile_user=profile_user,
                error_message=(
                    "Unable to upload image to Azurite storage. "
                    f"Error: {str(exc)}"
                ),
            ),
            status_code=500,
        )

    previous_blob_name = profile_user.get("profile_image_blob_name", "")
    if previous_blob_name:
        try:
            blob_container_client.delete_blob(previous_blob_name)
        except Exception:
            pass

    users_collection.update_one(
        {"_id": profile_user["_id"]},
        {
            "$set": {
                "profile_image_url": blob_client.url,
                "profile_image_blob_name": blob_name,
            }
        },
    )
    return RedirectResponse(url=f"/profile/{profile_user['username']}", status_code=303)


@app.get("/profile/{username}/photo-file")
async def serve_profile_photo(request: Request, username: str):
    viewer_document, redirect_response = _get_authenticated_user_or_redirect(request)
    if redirect_response is not None:
        return redirect_response

    profile_user = users_collection.find_one({"username": username.strip()})
    if profile_user is None:
        return Response(status_code=404)

    blob_name = profile_user.get("profile_image_blob_name", "")
    if not blob_name:
        return Response(status_code=404)

    blob_client = blob_container_client.get_blob_client(blob_name)
    try:
        downloader = blob_client.download_blob()
        content = downloader.readall()
        content_type = downloader.properties.content_settings.content_type or "application/octet-stream"
    except Exception:
        return Response(status_code=404)

    return Response(content=content, media_type=content_type)