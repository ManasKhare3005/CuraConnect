import os
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse
from dotenv import load_dotenv

from database.db import init_db
from agent import ConversationSession

load_dotenv()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="CuraConnect Voice Assistant", lifespan=lifespan)

# Serve static files and the frontend
app.mount("/static", StaticFiles(directory="web/static"), name="static")


@app.get("/")
async def root():
    return FileResponse("web/index.html")


@app.get("/favicon.ico")
async def favicon():
    return RedirectResponse(url="/static/favicon.svg", status_code=307)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    session = ConversationSession()

    try:
        # Send opening greeting
        greeting_text, greeting_audio = await session.start()
        await websocket.send_json({
            "type": "agent_message",
            "text": greeting_text,
            "audio": greeting_audio,
        })

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            msg_type = data.get("type")

            if msg_type == "user_message":
                user_text = data.get("text", "").strip()
                if not user_text:
                    continue

                location = data.get("location")  # {"latitude": ..., "longitude": ...}

                # Check if this message contains the user's name for the first time
                if session.user_id is None and session.user_name is None:
                    # Pass to agent — it will extract the name naturally
                    pass

                result = await session.process_message(user_text, location)

                # Send any side-effect events first (vital logged, doctors found)
                for event in result["events"]:
                    await websocket.send_json(event)

                # Send the agent's spoken response
                await websocket.send_json({
                    "type": "agent_message",
                    "text": result["text"],
                    "audio": result["audio"],
                })

            elif msg_type == "identify_user":
                # Frontend can call this once name is confirmed
                name = data.get("name", "")
                age = data.get("age")
                if name:
                    session.identify_user(name, age)

            elif msg_type == "location":
                lat = data.get("latitude")
                lng = data.get("longitude")
                if lat is not None and lng is not None:
                    session.set_location(lat, lng)

            elif msg_type == "address":
                address = data.get("address", "").strip()
                if address:
                    await session.set_address(address)

            elif msg_type == "end_session":
                session.end_session()
                break

    except WebSocketDisconnect:
        session.end_session()
    except Exception as e:
        print(f"WebSocket error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        session.end_session()
