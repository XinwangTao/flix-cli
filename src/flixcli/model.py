"""Bubble and intent wire models.

Facts (sources in reverse/protocol.md):
- Bubble JSON (verbatim-confirmed on the wire, official receiver log 9:09:53):
  {"id","from","to","type":0,"content":"...","time":<ms>,"groupId":""}
- type: 0=Text (db type distribution 0/1/2/3; 3=File per runtime log).
- FileTransfer content fields (official toString, runtime log):
  resourceId,name,mimeType,nameWithSuffix,size,path,width,height,
  thumbnailBase64,androidContentUri,extra,progress,speed,state,
  waitingForAccept,receiveBytes
- FileState observed: waitToAccepted, inTransit.
- /bubble response: 200 "received bubble".
"""
from __future__ import annotations

import json
import time
import uuid

BUBBLE_TEXT = 0
BUBBLE_FILE = 3

# FileState string values observed on the wire/log (official Dart enum names)
FILE_STATE_WAIT_TO_ACCEPTED = "waitToAccepted"
FILE_STATE_IN_TRANSIT = "inTransit"


def new_id() -> str:
    return str(uuid.uuid4())


def text_bubble(from_fp: str, to_fp: str, content: str, group_id: str = "") -> dict:
    return {
        "id": new_id(),
        "from": from_fp,
        "to": to_fp,
        "type": BUBBLE_TEXT,
        "content": content,
        "time": int(time.time() * 1000),
        "groupId": group_id,
    }


# --- File bubble (wire-verified verbatim from official _sendBasicBubble) -----
# content: {"state":<int>,"progress":0.0,"speed":0,"receiveBytes":0,
#           "meta":{"name","mimeType","nameWithSuffix","androidContentUri",
#                   "size","width","height","resourceId"},
#           "waitingForAccept":bool}
# FileState ints observed: 2=picked (sender), waitToAccepted/inTransit are the
# receiver-side names in official logs; db rows show 5/6 for received states.
# Data phase: sender POSTs /file?share_id=<resourceId>&file_name=<name>
# (chunked octet-stream) to the receiver, which replies 200.

FILE_STATE_PICKED = 2


def file_bubble(from_fp: str, to_fp: str, *, resource_id: str, name: str,
                mime_type: str, size: int, state: int = FILE_STATE_PICKED,
                waiting_for_accept: bool = True, group_id: str | None = None) -> dict:
    return {
        "id": new_id(),
        "from": from_fp,
        "to": to_fp,
        "type": BUBBLE_FILE,
        "content": {
            "state": state,
            "progress": 0.0,
            "speed": 0,
            "receiveBytes": 0,
            "meta": {
                "name": name,
                "mimeType": mime_type,
                "nameWithSuffix": name,
                "androidContentUri": "",
                "size": size,
                "width": 0,
                "height": 0,
                "resourceId": resource_id,
            },
            "waitingForAccept": waiting_for_accept,
        },
        "time": int(time.time() * 1000),
        "groupId": group_id,
    }


def dumps(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# --- TransIntent (wire-verified against official client, 2026-10-21) --------
# POST /intent body, verbatim capture:
# {"deviceId":"<sender fingerprint>","bubbleId":"","action":"clipboard",
#  "extra":{"text":"{\"text\":\"...\",\"time\":1792554971152}"}}
# Success response: 200, literal text `{0: success}` (Dart map toString).

ACTION_CLIPBOARD = "clipboard"


def clipboard_intent(from_fp: str, text: str, bubble_id: str = "") -> dict:
    inner = json.dumps({"text": text, "time": int(time.time() * 1000)},
                       separators=(",", ":"), ensure_ascii=False)
    return {
        "deviceId": from_fp,
        "bubbleId": bubble_id,
        "action": ACTION_CLIPBOARD,
        "extra": {"text": inner},
    }


# --- /ping_v2 manual-add handshake (wire-verified vs official, 2026-10-21) ---
# Manual "add device" sends POST /ping_v2 {"deviceModal":{...,"from":"manual"}}
# (official retries 3x on no answer). The server ignores the body, answers
# 200 text/plain with ITS OWN deviceModal double-encoded like the UDP
# announce: json.dumps(json.dumps(modal)). `from` is echoed from the request;
# an empty deviceModal -> 400 "Bad Request".

def device_modal(ident: dict, port: int, from_: str = "",
                 version: int = 1) -> dict:
    return {
        "alias": ident.get("alias", ""),
        "deviceModel": ident.get("deviceModel", "Windows"),
        "deviceType": ident.get("deviceType", "desktop"),
        "fingerprint": ident.get("fingerprint", ""),
        "port": port,
        "version": version,
        "ip": "",
        "host": "",
        "from": from_,
        "os": ident.get("os", "windows"),
    }


def ping_v2_body(ident: dict, port: int, from_: str = "manual",
                 version: int = 1) -> dict:
    return {"deviceModal": device_modal(ident, port, from_=from_,
                                        version=version)}


def parse_ping_v2_response(text: str) -> dict:
    """Double-encoded modal string -> modal dict (ValueError on junk)."""
    return json.loads(json.loads(text))


# --- pairing via TransIntent (wire-verified vs official, 2026-10-21) ---------
# Pairing DOES ride /intent (unlike earlier guesses): TransAction members
# include askPairDevice / confirmPairDevice [RT log verbatim
# "action = TransAction.askPairDevice"]. Semantics observed:
#   A->B askPairDevice  extra={verifyDevice:<B fp>, verifyCode:<B 4-digit key>}
#        -> B writes pair_devices[A_fp, code=<B's own key>] and answers
#           `{0: success}` (Dart map toString; errors {301: pair Device Null}
#           when verifyDevice missing, {302: device not match ...} when
#           verifyDevice != receiver's own fp).
#   B->A confirmPairDevice extra={verifyDevice:<A fp>, verifyCode, accept:true}
# pair_devices.code stores the WRITER's own pairing key. verifyCode is NOT
# actually validated by official 2.4.0+127 (wrong code also pairs) [RT].

ACTION_ASK_PAIR = "askPairDevice"
ACTION_CONFIRM_PAIR = "confirmPairDevice"
ACTION_CONFIRM_RECEIVE = "confirmReceive"
# Group metadata snapshot (v2C). `rd` prefix keeps it clear of the official
# TransAction names: an official peer answers 500 and the sender degrades
# silently, so group chat itself is never blocked by metadata sync.
ACTION_GROUP_SYNC = "rdGroupSync"
# Agent-to-agent envelope (v2K). Same `rd` rationale: an older peer answers 500
# and the sender degrades to "queued" instead of pretending it was delivered.
ACTION_AGENT = "rdAgent"


def agent_intent(from_fp: str, to_fp: str, mid: str, body: str,
                 reply_to: str = "", created: int = 0) -> dict:
    return {
        "action": ACTION_AGENT,
        "deviceId": from_fp,
        "extra": {"to": to_fp, "id": mid, "body": body,
                  "replyTo": reply_to, "created": created},
    }


def group_sync_intent(from_fp: str, group_id: str, name: str,
                      members: list[str], rev: int) -> dict:
    return {
        "deviceId": from_fp,
        "bubbleId": "",
        "action": ACTION_GROUP_SYNC,
        "extra": {
            "groupId": group_id,
            "name": name,
            "members": list(members),
            "rev": int(rev),
        },
    }


def confirm_receive_intent(from_fp: str, bubble_id: str) -> dict:
    """Receiver -> sender ack [RT wire_capture verbatim]: deviceId is the
    RECEIVER's fingerprint, bubbleId carries the file bubble id, extra={}."""
    return {
        "deviceId": from_fp,
        "bubbleId": bubble_id,
        "action": ACTION_CONFIRM_RECEIVE,
        "extra": {},
    }


def pair_intent(from_fp: str, peer_fp: str, peer_code: str,
                action: str = ACTION_ASK_PAIR, accept: bool = True) -> dict:
    extra = {"verifyDevice": peer_fp, "verifyCode": str(peer_code)}
    if action == ACTION_CONFIRM_PAIR:
        extra["accept"] = bool(accept)
    return {
        "deviceId": from_fp,
        "bubbleId": "",
        "action": action,
        "extra": extra,
    }


def parse_intent_reply(text: str) -> tuple[int, str]:
    """Official /intent replies are Dart map toString: `{0: success}`,
    `{301: pair Device Null}`, `{302: device not match ...}`."""
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        body = t[1:-1]
        code, _, msg = body.partition(": ")
        try:
            return int(code.strip()), msg.strip()
        except ValueError:
            return -1, t
    return -1, t
