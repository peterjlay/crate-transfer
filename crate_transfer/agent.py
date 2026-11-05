from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import secrets
import shutil
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Settings, atomic_json
from .identity import client_context, identity
from .library import Library, assert_serato_closed, sha256_file
from .protocol import MAX_JSON, ProtocolError, identifier, label, machine_id, validate_manifest

LOG = logging.getLogger(__name__)


class TransferError(RuntimeError):
    pass


class ImportFailed(TransferError):
    pass


class Agent:
    def __init__(self, settings: Settings, notify=lambda: None, guard=assert_serato_closed):
        self.settings, self.notify, self.guard = settings, notify, guard
        self.context, self.fingerprint = identity(settings.directory)
        self.lock = threading.RLock()
        self.jobs_lock = threading.RLock()
        self.session = None
        self.pair_code, self.pair_until, self.pair_attempts = "", 0.0, 0
        self.jobs_path = settings.directory / "transfers.json"
        self.jobs = []
        if self.jobs_path.exists():
            with self.jobs_path.open(encoding="utf-8") as source:
                self.jobs = json.load(source)
            for job in self.jobs:
                if job["status"] not in ("complete", "failed", "uncertain"):
                    was_importing = job["status"] == "importing"
                    job.update(status="uncertain" if was_importing else "failed",
                               detail=("Interrupted during import: inspect Serato and the backup before resending."
                                       if was_importing else "Interrupted by agent shutdown. Send again manually."))
            self.save_jobs()
        incoming = Path(settings.snapshot()["music"]).expanduser().resolve() / ".crate-transfer-incoming"
        for job in self.jobs:
            job_id = job["id"]
            if len(job_id) == 32 and all(c in "0123456789abcdef" for c in job_id):
                abandoned = incoming / job_id
                if abandoned.is_dir() and not abandoned.is_symlink():
                    shutil.rmtree(abandoned)
        self.server = None

    def save_jobs(self):
        atomic_json(self.jobs_path, self.jobs)

    def history(self):
        with self.jobs_lock:
            return json.loads(json.dumps(self.jobs))

    def job(self, job_id, **values):
        with self.jobs_lock:
            current = next((job for job in self.jobs if job["id"] == job_id), None)
            if current is None:
                current = {"id": job_id, "created": time.time()}
                self.jobs.insert(0, current)
            current.update(values)
            self.save_jobs()
        self.notify()

    def pairing(self) -> str:
        with self.lock:
            self.pair_code = f"{secrets.randbelow(1000000):06d}"
            self.pair_until = time.monotonic() + 120
            self.pair_attempts = 0
            return self.pair_code

    def pair(self, payload, host=None):
        with self.lock:
            if not isinstance(payload, dict):
                raise ProtocolError("Invalid pairing request.")
            if time.monotonic() > self.pair_until or self.pair_attempts >= 5:
                raise ProtocolError("Pairing is closed. Generate a new code on the receiving computer.")
            self.pair_attempts += 1
            if not hmac.compare_digest(str(payload.get("code", "")), self.pair_code):
                raise ProtocolError("Incorrect pairing code.")
            peer_id = machine_id(payload.get("id"))
            name = label(payload.get("name"), 100)
            reverse = payload.get("reverse")
            if reverse is not None:
                if not isinstance(reverse, dict) or not host:
                    raise ProtocolError("Invalid bidirectional pairing request.")
                reverse_fingerprint = identifier(reverse.get("fingerprint"))
                reverse_token = identifier(reverse.get("token"))
                reverse_port = reverse.get("port")
                if type(reverse_port) is not int or not 1024 <= reverse_port <= 65535:
                    raise ProtocolError("Invalid computer connection port.")
            token = secrets.token_hex(32)
            settings = self.settings.snapshot()
            settings["trusted"][peer_id] = {"name": name, "token_hash": hashlib.sha256(token.encode()).hexdigest()}
            if reverse is not None:
                settings["peers"][peer_id] = {
                    "id": peer_id, "name": name, "host": host, "port": reverse_port,
                    "fingerprint": reverse_fingerprint, "token": reverse_token,
                }
            self.settings.update(trusted=settings["trusted"], peers=settings["peers"])
            self.pair_until = 0
            return {"token": token, "id": settings["id"], "name": settings["name"],
                    "bidirectional": reverse is not None}

    def authorize(self, token: str) -> str:
        digest = hashlib.sha256(token.encode()).hexdigest()
        for peer_id, peer in self.settings.snapshot()["trusted"].items():
            if hmac.compare_digest(peer["token_hash"], digest):
                return peer_id
        raise PermissionError("Computer not paired, or access revoked.")

    def _expire(self):
        if self.session and time.monotonic() - self.session["touched"] > 3600:
            session = self.session
            self.job(session["id"], status="failed", detail="Receive session expired after one hour of inactivity.")
            shutil.rmtree(session["staging"])
            self.session = None

    def begin(self, manifest: dict, peer_id: str) -> dict:
        validate_manifest(manifest)
        if manifest["sender_id"] != peer_id:
            raise PermissionError("Manifest computer identity does not match the paired computer.")
        with self.lock:
            self._expire()
            if self.session:
                raise TransferError("The other computer is busy. Finish or cancel its current transfer first.")
            self.guard()
            settings = self.settings.snapshot()
            library = Library(settings["library"])
            music = Path(settings["music"]).expanduser().resolve()
            library.validate_destination(music)
            music.mkdir(parents=True, exist_ok=True)
            required = sum(item["size"] for item in manifest["files"])
            backup_size = sum((library.directory / filename).stat().st_size
                              for filename in ("root.sqlite", "master.sqlite"))
            same_volume = os.stat(music).st_dev == os.stat(self.settings.directory).st_dev
            overhead = backup_size if same_volume else 0
            if (shutil.disk_usage(music).free < required + overhead + 256 * 1024**2
                    or shutil.disk_usage(self.settings.directory).free < backup_size + 256 * 1024**2):
                raise TransferError("Not enough free space for the music, database backups and a 256 MiB safety margin.")
            session_id = uuid.uuid4().hex
            # Stage on the destination volume, so publishing files is atomic.
            staging = music / ".crate-transfer-incoming" / session_id
            staging.mkdir(parents=True, mode=0o700)
            self.session = {"id": session_id, "peer": peer_id, "manifest": manifest, "music": music,
                            "library": library, "staging": staging, "received": set(),
                            "touched": time.monotonic()}
            self.job(session_id, direction="receive", peer=manifest["sender_name"], status="receiving",
                     owner=peer_id, detail=f"Waiting for {len(manifest['files'])} files", progress=0)
            return {"id": session_id}

    def require_session(self, session_id, peer_id):
        self._expire()
        if not self.session or self.session["id"] != session_id or self.session["peer"] != peer_id:
            raise TransferError("Unknown, expired, or unauthorized receive session.")
        self.session["touched"] = time.monotonic()
        return self.session

    def upload(self, session_id, key, peer_id, length, source):
        identifier(key)
        with self.lock:
            session = self.require_session(session_id, peer_id)
            item = next((item for item in session["manifest"]["files"] if item["id"] == key), None)
            if item is None or length != item["size"]:
                raise ProtocolError("Unexpected file or incorrect Content-Length.")
            destination = session["staging"] / key
            digest = hashlib.sha256()
            try:
                with destination.open("wb") as target:
                    remaining = length
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise TransferError("Connection ended before the entire file arrived.")
                        target.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if not hmac.compare_digest(digest.hexdigest(), item["sha256"]):
                    raise TransferError("File checksum failed. Source may have changed; send the crate again.")
            except BaseException:
                destination.unlink(missing_ok=True)
                session["received"].discard(key)
                raise
            session["received"].add(key)
            session["touched"] = time.monotonic()
            total = sum(item["size"] for item in session["manifest"]["files"])
            received = sum(item["size"] for item in session["manifest"]["files"] if item["id"] in session["received"])
            self.job(session_id, progress=received / max(total, 1),
                     detail=f"{len(session['received'])} / {len(session['manifest']['files'])} files verified")

    def finish(self, session_id, peer_id):
        with self.lock:
            for job in self.jobs:
                if job["id"] == session_id and job.get("owner") == peer_id and job["status"] == "complete":
                    return job["result"]
            session = self.require_session(session_id, peer_id)
            manifest = session["manifest"]
            if len(session["received"]) != len(manifest["files"]):
                raise TransferError("Not all files have been verified.")
            self.guard()
            installed, created, directories = {}, [], []
            committed = False
            try:
                file_crates = {}
                for crate in manifest["crates"]:
                    for key in crate["tracks"]:
                        file_crates.setdefault(key, crate["path"])
                for item in manifest["files"]:
                    folder = session["music"]
                    for part in file_crates[item["id"]]:
                        safe = "".join("_" if c in '/\\<>:"|?*' else c for c in part).rstrip(". ")
                        safe = safe.encode("utf-8")[:180].decode("utf-8", errors="ignore")
                        if not safe or safe in (".", "..") or safe.startswith(".") or safe.split(".")[0].upper() in {
                            "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                            *(f"LPT{i}" for i in range(1, 10)),
                        }:
                            safe = "_" + safe
                        folder = folder / safe
                        if folder.is_symlink():
                            raise TransferError("Destination contains an unexpected symbolic link.")
                        if not folder.exists():
                            folder.mkdir()
                            directories.append(folder)
                    destination = folder / item["name"]
                    number = 2
                    while destination.exists() or destination.is_symlink():
                        if destination.is_symlink():
                            raise TransferError("Destination contains an unexpected symbolic link.")
                        if destination.is_file() and sha256_file(destination) == item["sha256"]:
                            break
                        original = Path(item["name"])
                        destination = folder / f"{original.stem} ({number}){original.suffix}"
                        number += 1
                    if destination.exists():
                        if sha256_file(destination) != item["sha256"]:
                            raise TransferError(f"Existing file changed: {destination}. It will not be overwritten.")
                    else:
                        os.link(session["staging"] / item["id"], destination)
                        created.append(destination)
                    installed[item["id"]] = destination
                self.job(session_id, status="importing", detail="Backing up and importing Serato 4 library")
                result = session["library"].import_crates(
                    manifest, installed, self.settings.directory / "backups" / session_id,
                    session_id, guard=self.guard,
                )
                committed = True
                self.job(session_id, owner=peer_id, status="complete", progress=1,
                         detail=f"Imported {result['crates']} crates. Open Serato to load them.", result=result)
                return result
            except BaseException as error:
                if not committed:
                    for path in created:
                        path.unlink()
                    for directory in reversed(directories):
                        if not any(directory.iterdir()):
                            directory.rmdir()
                self.job(session_id, status="uncertain" if committed else "failed", detail=str(error))
                raise
            finally:
                try:
                    shutil.rmtree(session["staging"])
                except OSError as error:
                    LOG.exception("Could not remove upload staging after import")
                    current = next(job for job in self.history() if job["id"] == session_id)
                    self.job(session_id, detail=current["detail"] + f" Staging cleanup failed: {error}")
                finally:
                    self.session = None

    def cancel(self, session_id, peer_id):
        with self.lock:
            session = self.require_session(session_id, peer_id)
            shutil.rmtree(session["staging"])
            self.job(session_id, status="failed", detail="Transfer cancelled. No crates imported.")
            self.session = None

    def start(self, host="0.0.0.0", port=None):
        self.server = Server((host, self.settings.snapshot()["port"] if port is None else port), Handler)
        self.server.agent = self
        self.server.context = self.context
        threading.Thread(target=self.server.serve_forever, name="crate-receiver", daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(45)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request = self.context.wrap_socket(request, server_side=True)
            super().process_request_thread(request, client_address)
        except (ssl.SSLError, OSError):
            request.close()
            LOG.debug("TLS connection closed", exc_info=True)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "CrateTransfer/1"
    sys_version = ""

    def log_message(self, format, *args):
        LOG.debug(format, *args)

    def respond(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def body_length(self):
        if self.headers.get("Transfer-Encoding"):
            raise ProtocolError("Chunked requests are not supported.")
        raw = self.headers.get("Content-Length", "")
        if not raw.isdigit():
            raise ProtocolError("Content-Length is required.")
        return int(raw)

    def read_json(self):
        length = self.body_length()
        if length > MAX_JSON:
            raise ProtocolError("Request manifest is too large.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ProtocolError("Incomplete JSON request.")
        return json.loads(raw)

    def dispatch(self):
        agent = self.server.agent
        if self.command == "GET" and self.path == "/v1/info":
            settings = agent.settings.snapshot()
            return {"version": 1, "id": settings["id"], "name": settings["name"], "pairing_version": 2}
        if self.command == "POST" and self.path == "/v1/pair":
            return agent.pair(self.read_json(), self.client_address[0])
        authorization = self.headers.get("Authorization", "")
        peer = agent.authorize(authorization.removeprefix("Bearer "))
        parts = self.path.strip("/").split("/")
        if self.command == "POST" and parts == ["v1", "transfers"]:
            return agent.begin(self.read_json(), peer)
        if len(parts) >= 3 and parts[:2] == ["v1", "transfers"]:
            session_id = parts[2]
            if self.command == "GET" and len(parts) == 3:
                job = next((job for job in agent.history()
                            if job["id"] == session_id and job.get("owner") == peer), None)
                if job is None:
                    raise ProtocolError("Unknown transfer for this computer.")
                return {key: job[key] for key in ("status", "detail", "result") if key in job}
            if self.command == "PUT" and len(parts) == 5 and parts[3] == "files":
                agent.upload(session_id, parts[4], peer, self.body_length(), self.rfile)
                return {"verified": True}
            if self.command == "POST" and len(parts) == 4 and parts[3] == "finish":
                return agent.finish(session_id, peer)
            if self.command == "DELETE" and len(parts) == 3:
                agent.cancel(session_id, peer)
                return {"cancelled": True}
        raise ProtocolError("Unknown endpoint.")

    def handle_request(self):
        try:
            result = self.dispatch()
        except PermissionError as error:
            self.respond(403, {"error": str(error)})
        except (ValueError, TransferError, OSError) as error:
            LOG.warning("Request rejected: %s", error)
            self.respond(400, {"error": str(error)})
        except Exception:
            LOG.exception("Receiver request failed")
            self.respond(500, {"error": "Remote operation failed. See Activity and the application log on that computer."})
        else:
            self.respond(200, result)

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request


class Client:
    def __init__(self, host: str, port: int, fingerprint: str, token=""):
        self.host, self.port, self.token = host, port, token
        self.fingerprint = identifier(fingerprint.lower().replace(":", "").replace(" ", ""))

    def request(self, method, endpoint, payload=None, file: Path | None = None, progress=lambda count: None,
                timeout=60):
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=timeout, context=client_context())
        try:
            connection.connect()
            actual = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(actual, self.fingerprint):
                raise TransferError("Computer fingerprint does not match. No credentials or music were sent. "
                                    "Check the computer address, then pair again with a fresh code on a trusted network "
                                    "if its identity has changed.")
            body = json.dumps(payload).encode() if payload is not None else b""
            headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
            if self.token:
                headers["Authorization"] = "Bearer " + self.token
            if file is not None:
                size = file.stat().st_size
                headers["Content-Length"] = str(size)
                headers["Content-Type"] = "application/octet-stream"
                connection.putrequest(method, endpoint)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.endheaders()
                with file.open("rb") as source:
                    sent = 0
                    while sent < size:
                        chunk = source.read(min(1024 * 1024, size - sent))
                        if not chunk:
                            raise TransferError("Source file changed during transfer.")
                        connection.send(chunk)
                        sent += len(chunk)
                        progress(len(chunk))
            else:
                connection.request(method, endpoint, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_JSON + 1)
            if len(raw) > MAX_JSON:
                raise TransferError("Remote response is too large.")
            result = json.loads(raw)
            if response.status != 200:
                raise TransferError(result.get("error", f"The other computer returned HTTP {response.status}"))
            return result
        finally:
            connection.close()


def pair_computer(settings: Settings, host, port, fingerprint, code):
    if fingerprint is None:
        # Bootstrap only during explicit code pairing; transfers always use the saved pin.
        connection = http.client.HTTPSConnection(host, port, timeout=60, context=client_context())
        try:
            connection.connect()
            fingerprint = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        finally:
            connection.close()
    client = Client(host, port, fingerprint)
    if client.request("GET", "/v1/info").get("pairing_version") != 2:
        raise TransferError("Update Crate Transfer on the other computer to enable send-and-receive pairing.")
    own = settings.snapshot()
    _, own_fingerprint = identity(settings.directory)
    reverse_token = secrets.token_hex(32)
    result = client.request("POST", "/v1/pair", {
        "id": own["id"], "name": own["name"], "code": code,
        "reverse": {"fingerprint": own_fingerprint, "token": reverse_token, "port": own["port"]},
    })
    if result.get("bidirectional") is not True:
        raise TransferError("The other computer did not confirm send-and-receive pairing. Update both apps and pair again.")
    peer_id = machine_id(result["id"])
    own["peers"][peer_id] = {"id": peer_id, "name": label(result["name"], 100), "host": host, "port": port,
                             "fingerprint": client.fingerprint, "token": identifier(result["token"])}
    own["trusted"][peer_id] = {"name": own["peers"][peer_id]["name"],
                              "token_hash": hashlib.sha256(reverse_token.encode()).hexdigest()}
    settings.update(peers=own["peers"], trusted=own["trusted"])


def send_crates(agent: Agent, selected: list[int], peer_id: str, cancelled=lambda: False):
    job_id = uuid.uuid4().hex
    own = agent.settings.snapshot()
    peer = own["peers"][peer_id]
    client = Client(peer["host"], peer["port"], peer["fingerprint"], peer["token"])
    agent.job(job_id, direction="send", peer=peer["name"], peer_id=peer_id, status="preparing", progress=0,
              detail="Reading selected crates and checking files")
    remote_id, finishing = None, False
    try:
        def check_cancelled():
            if cancelled():
                raise TransferError("Transfer cancelled by this computer. No crates imported.")

        def preparing(text):
            check_cancelled()
            agent.job(job_id, detail=text)

        manifest, paths = Library(own["library"]).export(selected, own["id"], own["name"],
                                                       preparing)
        check_cancelled()
        validate_manifest(manifest)
        if len(json.dumps(manifest).encode()) > MAX_JSON:
            raise ProtocolError("Selected crates exceed the 8 MiB manifest limit. Send fewer crates.")
        remote_id = client.request("POST", "/v1/transfers", manifest)["id"]
        total, sent = sum(item["size"] for item in manifest["files"]), 0
        last_report = 0.0

        def progress(count):
            nonlocal sent, last_report
            check_cancelled()
            sent += count
            if time.monotonic() - last_report > 0.2:
                last_report = time.monotonic()
                agent.job(job_id, progress=sent / max(total, 1))

        for index, item in enumerate(manifest["files"]):
            check_cancelled()
            agent.job(job_id, status="sending", detail=f"{index + 1}/{len(paths)}  {item['name']}")
            client.request("PUT", f"/v1/transfers/{remote_id}/files/{item['id']}",
                           file=paths[item["id"]], progress=progress)
        check_cancelled()
        agent.job(job_id, status="importing", detail="The other computer is backing up and importing the crates", progress=1,
                  remote_id=remote_id)
        finishing = True
        try:
            result = client.request("POST", f"/v1/transfers/{remote_id}/finish", timeout=600)
        except (OSError, http.client.HTTPException):
            agent.job(job_id, detail="Connection interrupted during import. Checking the other computer; do not resend.",
                      remote_id=remote_id)
            result = recover_receipt(client, remote_id)
        agent.job(job_id, status="complete", detail=f"{result['crates']} crates delivered. Open Serato on {peer['name']}.",
                  result=result, remote_id=remote_id)
        return result
    except Exception as error:
        LOG.exception("Transfer failed")
        detail = str(error)
        uncertain = finishing and not isinstance(error, ImportFailed)
        if uncertain:
            detail += " Import outcome may be unknown; check Activity on the other computer before resending."
        agent.job(job_id, status="uncertain" if uncertain else "failed", detail=detail, remote_id=remote_id)
        if remote_id and not finishing:
            try:
                client.request("DELETE", f"/v1/transfers/{remote_id}")
            except Exception:
                LOG.exception("Could not cancel remote receive session; cancel it on the receiver")
        raise


def recover_receipt(client: Client, remote_id: str, wait_seconds=600):
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            status = client.request("GET", f"/v1/transfers/{remote_id}", timeout=10)
        except (OSError, http.client.HTTPException) as error:
            LOG.warning("Could not check remote import outcome: %s", error)
        else:
            if status["status"] == "complete":
                return status["result"]
            if status["status"] == "failed":
                raise ImportFailed(status["detail"])
            if status["status"] == "uncertain":
                raise TransferError(status["detail"])
        if time.monotonic() >= deadline:
            raise TransferError("Could not confirm the remote import. Check Activity on the other computer before resending.")
        time.sleep(2)


def check_transfer(agent: Agent, job_id: str):
    job = next(job for job in agent.history() if job["id"] == job_id)
    if job.get("direction") != "send" or not job.get("remote_id") or job["status"] != "uncertain":
        raise TransferError("Select an uncertain outgoing transfer with a remote transfer ID.")
    peers = agent.settings.snapshot()["peers"]
    peer_id = job.get("peer_id")
    if peer_id is None:
        matches = [key for key, peer in peers.items() if peer["name"] == job["peer"]]
        if len(matches) != 1:
            raise TransferError("Cannot uniquely identify the paired computer for this older transfer.")
        peer_id = matches[0]
    if peer_id not in peers:
        raise TransferError("Pair with the original computer again before checking this transfer.")
    peer = peers[peer_id]
    try:
        receipt = recover_receipt(Client(peer["host"], peer["port"], peer["fingerprint"], peer["token"]),
                                  job["remote_id"], wait_seconds=0)
    except ImportFailed as error:
        agent.job(job_id, status="failed", detail=str(error))
        raise
    agent.job(job_id, status="complete", progress=1, result=receipt,
              detail=f"Confirmed {receipt['crates']} crates delivered. No files were resent.")
    return receipt
