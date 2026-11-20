"""Administer the gated macOS helper without changing the user's Gitgo app.

Uninstall removes the resident service and client credential. Immutable runtime
versions and private recovery records remain for shared registrations/auditing.
Project authority remains boot/expiry bounded; residency does not renew it.
"""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.core.macos_helper_admin import main


if __name__=='__main__': raise SystemExit(main())
