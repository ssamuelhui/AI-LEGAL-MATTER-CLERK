r"""First-run configuration wizard (Phase 3 Session 5).

A small tkinter dialog that collects the two API keys and writes them to
`<data_dir>/.env`. Shown by the installer's post-install step, and by the
launcher itself whenever `.env` is absent.

**Why tkinter and not a Flask page.** The wizard has to appear at the one
moment the user has no way to recover if it does not. A browser-served wizard
depends on `webbrowser.open()` succeeding from a shell-launch context -- which
is exactly the situation docs/BACKLOG.md records as intermittently failing --
and the installer's "run now" step and the Start Menu shortcut are both that
context. tkinter draws its own window with no browser in the loop. It looks
dated; it appears every time.

The wizard runs in the SAME process as the app (`MatterClerk.exe --first-run`),
and on success simply returns True so the launcher falls through into normal
startup. No subprocess, no second console, nothing to orphan.
"""

from __future__ import annotations

import os
import queue
import threading
import webbrowser
from pathlib import Path

import requests

from .paths import data_dir

DEFAULT_MODEL = "xiaomi/mimo-v2.5-pro"

OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"
# GET /key, not GET /models. /models is unauthenticated -- it answers 200 for a
# key that is empty, expired or fabricated, so testing against it would tell a
# lawyer their bad key is fine. /key requires the bearer token and is the only
# cheap endpoint that actually proves the credential works.
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

CANLII_TEST_URL = "https://api.canlii.org/v1/caseBrowse/en/"

TEST_TIMEOUT_SECONDS = 20


# --------------------------------------------------------------------------
# Connection tests (no tkinter -- importable and testable headlessly)
# --------------------------------------------------------------------------
def test_openrouter(key: str) -> tuple[bool, str]:
    """Validate an OpenRouter key. Returns (ok, message)."""
    key = (key or "").strip()
    if not key:
        return False, "No key entered."
    try:
        r = requests.get(
            OPENROUTER_KEY_URL,
            headers={"Authorization": f"Bearer {key}"},
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        # Distinguished from a rejected key on purpose: "you are offline" and
        # "your key is wrong" call for completely different actions, and a
        # lawyer behind a corporate proxy will hit the former constantly.
        return False, f"Could not reach OpenRouter ({type(e).__name__}). Check your internet connection or proxy."

    if r.status_code in (401, 403):
        return False, f"Key rejected by OpenRouter (HTTP {r.status_code}). Check you copied the whole key."
    if r.status_code != 200:
        return False, f"OpenRouter returned HTTP {r.status_code}."

    # Deliberately NOT echoing the key's label: OpenRouter defaults it to a
    # masked form of the key itself, so showing it would print a credential
    # fragment into a status line on a legal practitioner's screen.
    detail = ""
    try:
        data = (r.json() or {}).get("data") or {}
        limit = data.get("limit")
        if limit is not None:
            detail = f" Credit limit: {limit}."
    except ValueError:
        pass

    return True, f"Key valid.{detail}"


def test_canlii(key: str) -> tuple[bool, str]:
    """Validate a CanLII key. Returns (ok, message)."""
    key = (key or "").strip()
    if not key:
        return False, "No key entered (optional -- case-law tasks will be unavailable)."
    try:
        r = requests.get(
            CANLII_TEST_URL,
            params={"api_key": key},
            timeout=TEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as e:
        return False, f"Could not reach CanLII ({type(e).__name__}). Check your internet connection or proxy."

    if r.status_code in (401, 403):
        return False, f"Key rejected by CanLII (HTTP {r.status_code})."
    if r.status_code != 200:
        return False, f"CanLII returned HTTP {r.status_code}."

    count = None
    try:
        payload = r.json() or {}
        dbs = payload.get("caseDatabases")
        if isinstance(dbs, list):
            count = len(dbs)
    except ValueError:
        pass

    detail = f" ({count} case databases reachable)" if count else ""
    return True, f"Key valid{detail}."


# --------------------------------------------------------------------------
# .env writing
# --------------------------------------------------------------------------
def env_path() -> Path:
    return data_dir() / ".env"


def needs_wizard() -> bool:
    """True when no .env exists in the data directory."""
    if os.environ.get("MATTER_CLERK_SKIP_WIZARD"):
        return False
    return not env_path().is_file()


def write_env(openrouter_key: str, canlii_key: str, model: str = DEFAULT_MODEL) -> Path:
    """Write .env to the data directory and restrict it to the current user.

    The file holds live API credentials, so it is created with an owner-only
    ACL rather than inheriting the parent directory's permissions.
    """
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    body = "\n".join([
        "# Written by the Matter Clerk first-run wizard.",
        "# These keys are stored only on this computer.",
        "",
        f"OPENROUTER_API_KEY={openrouter_key.strip()}",
        f"MODEL={model}",
        f"CANLII_API_KEY={canlii_key.strip()}",
        "",
    ])
    path.write_text(body, encoding="utf-8")
    _restrict_to_current_user(path)
    return path


def _restrict_to_current_user(path: Path) -> None:
    """Best-effort owner-only ACL. Never fatal -- a readable .env beats no .env."""
    try:
        import subprocess

        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# The dialog
# --------------------------------------------------------------------------
def run_wizard() -> bool:
    """Show the wizard. Returns True to continue starting the app, False to quit.

    Imports tkinter lazily so that headless use of this module (the connection
    tests, .env writing) works on a machine with no display libraries.
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    state = {"saved": False, "tested": False, "all_passed": False}
    results: queue.Queue = queue.Queue()

    root = tk.Tk()
    root.title("Matter Clerk - Setup")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=18)
    frame.grid(sticky="nsew")

    row = 0
    ttk.Label(
        frame,
        text="Matter Clerk needs two API keys before it can draft or research.",
        font=("Segoe UI", 10, "bold"),
    ).grid(row=row, column=0, columnspan=3, sticky="w")

    row += 1
    ttk.Label(
        frame,
        text=f"Keys are stored only on this computer, in\n{env_path()}",
        foreground="#555555",
        justify="left",
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 14))

    # --- OpenRouter --------------------------------------------------------
    row += 1
    ttk.Label(frame, text="OpenRouter API key", font=("Segoe UI", 9, "bold")).grid(
        row=row, column=0, sticky="w"
    )
    link = ttk.Label(frame, text="Get a key", foreground="#0645ad", cursor="hand2")
    link.grid(row=row, column=2, sticky="e")
    link.bind("<Button-1>", lambda _e: webbrowser.open(OPENROUTER_KEYS_URL))

    row += 1
    or_var = tk.StringVar()
    or_entry = ttk.Entry(frame, textvariable=or_var, width=58, show="*")
    or_entry.grid(row=row, column=0, columnspan=2, sticky="we", pady=(2, 0))

    or_show = tk.BooleanVar(value=False)

    def _toggle_or() -> None:
        or_entry.configure(show="" if or_show.get() else "*")

    ttk.Checkbutton(frame, text="Show", variable=or_show, command=_toggle_or).grid(
        row=row, column=2, sticky="w", padx=(8, 0), pady=(2, 0)
    )

    row += 1
    ttk.Label(
        frame,
        text="Required. Used for drafting and analysis.",
        foreground="#555555",
    ).grid(row=row, column=0, columnspan=3, sticky="w")

    row += 1
    or_status = ttk.Label(frame, text="", justify="left", wraplength=480)
    or_status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 12))

    # --- CanLII ------------------------------------------------------------
    row += 1
    ttk.Label(frame, text="CanLII API key", font=("Segoe UI", 9, "bold")).grid(
        row=row, column=0, sticky="w"
    )

    row += 1
    cl_var = tk.StringVar()
    cl_entry = ttk.Entry(frame, textvariable=cl_var, width=58, show="*")
    cl_entry.grid(row=row, column=0, columnspan=2, sticky="we", pady=(2, 0))

    cl_show = tk.BooleanVar(value=False)

    def _toggle_cl() -> None:
        cl_entry.configure(show="" if cl_show.get() else "*")

    ttk.Checkbutton(frame, text="Show", variable=cl_show, command=_toggle_cl).grid(
        row=row, column=2, sticky="w", padx=(8, 0), pady=(2, 0)
    )

    row += 1
    ttk.Label(
        frame,
        text="Optional. Contact CanLII directly to request an API key.\n"
             "Without it, case-law tasks are unavailable; everything else works.",
        foreground="#555555",
        justify="left",
    ).grid(row=row, column=0, columnspan=3, sticky="w")

    row += 1
    cl_status = ttk.Label(frame, text="", justify="left", wraplength=480)
    cl_status.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 14))

    # --- buttons -----------------------------------------------------------
    row += 1
    buttons = ttk.Frame(frame)
    buttons.grid(row=row, column=0, columnspan=3, sticky="we")

    test_btn = ttk.Button(buttons, text="Test connections")
    test_btn.grid(row=0, column=0, sticky="w")

    ttk.Frame(buttons).grid(row=0, column=1, sticky="we")
    buttons.columnconfigure(1, weight=1)

    save_btn = ttk.Button(buttons, text="Save and start", state="disabled")
    save_btn.grid(row=0, column=2, padx=(0, 6))

    cancel_btn = ttk.Button(buttons, text="Cancel")
    cancel_btn.grid(row=0, column=3)

    def _set_status(label: "ttk.Label", ok: bool, msg: str) -> None:
        label.configure(text=("PASS  " if ok else "FAIL  ") + msg,
                        foreground="#1a7f37" if ok else "#a40e26")

    def _on_key_change(*_a) -> None:
        save_btn.configure(state="normal" if or_var.get().strip() else "disabled")

    or_var.trace_add("write", _on_key_change)

    # --- testing, off the UI thread ---------------------------------------
    def _run_tests() -> None:
        test_btn.configure(state="disabled", text="Testing...")
        or_status.configure(text="Testing...", foreground="#555555")
        cl_status.configure(text="Testing...", foreground="#555555")

        or_key, cl_key = or_var.get(), cl_var.get()

        def worker() -> None:
            # Network calls must never run on the Tk thread; a 20 s timeout
            # would otherwise freeze the window and read as a hang.
            results.put(("openrouter", *test_openrouter(or_key)))
            if cl_key.strip():
                results.put(("canlii", *test_canlii(cl_key)))
            else:
                results.put(("canlii", None, "Not set. Case-law tasks will be unavailable."))
            results.put(("done", None, ""))

        threading.Thread(target=worker, daemon=True).start()
        root.after(100, _drain)

    def _drain() -> None:
        try:
            while True:
                which, ok, msg = results.get_nowait()
                if which == "done":
                    test_btn.configure(state="normal", text="Test connections")
                    state["tested"] = True
                    return
                label = or_status if which == "openrouter" else cl_status
                if ok is None:
                    label.configure(text=msg, foreground="#555555")
                else:
                    _set_status(label, ok, msg)
                    if which == "openrouter":
                        state["all_passed"] = bool(ok)
        except queue.Empty:
            pass
        root.after(100, _drain)

    def _on_save() -> None:
        if state["tested"] and not state["all_passed"]:
            if not messagebox.askyesno(
                "Save anyway?",
                "The OpenRouter key did not pass its test.\n\n"
                "Save it anyway? You can correct it later by editing\n"
                f"{env_path()}",
                default=messagebox.NO,
                parent=root,
            ):
                return
        try:
            write_env(or_var.get(), cl_var.get())
        except OSError as e:
            messagebox.showerror("Could not save", f"Failed to write {env_path()}:\n\n{e}", parent=root)
            return
        state["saved"] = True
        root.destroy()

    def _on_cancel() -> None:
        state["saved"] = False
        root.destroy()

    test_btn.configure(command=_run_tests)
    save_btn.configure(command=_on_save)
    cancel_btn.configure(command=_on_cancel)
    root.protocol("WM_DELETE_WINDOW", _on_cancel)

    or_entry.focus_set()

    # Centre on screen before showing, so it does not flash at the top-left.
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{max(0, x)}+{max(0, y)}")

    root.mainloop()
    return bool(state["saved"])
