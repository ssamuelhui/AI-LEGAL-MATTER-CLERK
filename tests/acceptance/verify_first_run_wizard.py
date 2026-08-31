r"""Phase 3 Session 5: first-run wizard checks that do not need a human.

Covers the parts of the wizard that are testable headlessly -- key validation,
.env writing, wizard-trigger detection -- plus a construct-and-destroy smoke
test of the tkinter dialog itself, which catches layout and widget-API errors
without anyone having to look at a window.

The appearance of the dialog, and the install/uninstall cycle, remain manual.

Run:  .venv\Scripts\python.exe tests\acceptance\verify_first_run_wizard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

BOGUS_KEY = "sk-or-v1-0000000000000000000000000000000000000000000000000000000000000000"

passed: list[str] = []
failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  [{'ok  ' if ok else 'FAIL'}] {name}{(' -- ' + detail) if detail else ''}")


def main() -> int:
    # Point the data directory at a scratch dir BEFORE importing the wizard,
    # so nothing here can touch the developer's real .env.
    tmp = tempfile.mkdtemp(prefix="mc_wizard_test_")
    os.environ["MATTER_CLERK_DATA_DIR"] = tmp
    os.environ.pop("MATTER_CLERK_SKIP_WIZARD", None)

    from matter_clerk import first_run_wizard as w

    print("\n1. WIZARD TRIGGER")
    check("needs_wizard() is True when .env is absent", w.needs_wizard())
    check("env_path() resolves under the data directory",
          str(w.env_path()) == str(Path(tmp) / ".env"), str(w.env_path()))

    print("\n2. .env WRITING")
    path = w.write_env("sk-or-test-key", "canlii-test-key")
    body = path.read_text(encoding="utf-8")
    check("file created", path.is_file(), str(path))
    check("OPENROUTER_API_KEY written", "OPENROUTER_API_KEY=sk-or-test-key" in body)
    check("CANLII_API_KEY written", "CANLII_API_KEY=canlii-test-key" in body)
    check("MODEL defaulted", f"MODEL={w.DEFAULT_MODEL}" in body)
    check("needs_wizard() is False once .env exists", not w.needs_wizard())
    check("keys are stripped of stray whitespace",
          "OPENROUTER_API_KEY=abc" in w.write_env("  abc  ", "").read_text(encoding="utf-8"))

    print("\n3. KEY VALIDATION -- rejects a bogus key")
    ok, msg = w.test_openrouter(BOGUS_KEY)
    check("bogus OpenRouter key is REJECTED", not ok, msg)
    ok, msg = w.test_openrouter("")
    check("empty OpenRouter key is rejected without a network call", not ok, msg)
    ok, msg = w.test_canlii("")
    check("empty CanLII key reports optional-not-set", not ok, msg)
    ok, msg = w.test_canlii("definitely-not-a-real-canlii-key")
    check("bogus CanLII key is REJECTED", not ok, msg)

    # Why /key and not /models: the brief specified /models, but that endpoint
    # is unauthenticated, so it answers 200 for any key at all. Demonstrated
    # rather than asserted, because it is the reason for the deviation.
    print("\n4. WHY NOT /models -- the endpoint the brief specified")
    try:
        import requests

        r = requests.get(w.OPENROUTER_MODELS_URL,
                         headers={"Authorization": f"Bearer {BOGUS_KEY}"}, timeout=20)
        check("/models would have WRONGLY passed the bogus key",
              r.status_code == 200,
              f"HTTP {r.status_code} -- confirms /models cannot validate a credential")
    except Exception as e:                                        # noqa: BLE001
        print(f"  [skip] /models comparison unavailable ({type(e).__name__})")

    print("\n5. KEY VALIDATION -- accepts the real key, if one is configured")
    real = ""
    real_env = ROOT / ".env"
    if real_env.is_file():
        for line in real_env.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                real = line.split("=", 1)[1].strip()
    if real:
        ok, msg = w.test_openrouter(real)
        check("real OpenRouter key is ACCEPTED", ok, msg)
    else:
        print("  [skip] no OPENROUTER_API_KEY in the repo .env to test against")

    print("\n6. DIALOG CONSTRUCTION (build and tear down, no interaction)")
    try:
        import tkinter as tk

        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
        have_display = True
    except Exception as e:                                        # noqa: BLE001
        have_display = False
        print(f"  [skip] no usable Tk display ({type(e).__name__})")

    if have_display:
        import threading

        # run_wizard() blocks in mainloop, so close it from a timer thread the
        # same way a user clicking the X would.
        import tkinter as tk

        result: dict = {}

        def closer() -> None:
            # Give mainloop a moment to come up, then find and destroy the
            # window -- exercising the real construction path end to end.
            import time
            time.sleep(2.5)
            for wdw in list(tk._default_root.children.values()) if tk._default_root else []:
                pass
            if tk._default_root is not None:
                tk._default_root.after(0, tk._default_root.destroy)

        threading.Thread(target=closer, daemon=True).start()
        try:
            result["saved"] = w.run_wizard()
            check("dialog builds, runs and closes cleanly", True,
                  f"returned saved={result['saved']} (False = treated as cancel)")
        except Exception as e:                                    # noqa: BLE001
            check("dialog builds, runs and closes cleanly", False,
                  f"{type(e).__name__}: {e}")

    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(passed)} passed, {len(failed)} failed")
    if failed:
        for f in failed:
            print(f"  failed: {f}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
