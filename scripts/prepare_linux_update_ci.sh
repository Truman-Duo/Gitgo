#!/usr/bin/env bash
# Prepare only the disposable hosted update worker; never relax global policy.
set -euo pipefail
if [[ "${GITHUB_ACTIONS:-}" != true || "${RUNNER_ENVIRONMENT:-}" != github-hosted ]]; then
  echo 'Linux update CI preparation requires a disposable GitHub-hosted runner' >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y bubblewrap gcc patchelf libseccomp2 apparmor-profiles

# Ubuntu 24.04 can permit userns creation but deny namespace-local capabilities.
# Load the distribution's bwrap setup/child-restriction profile, preserving its
# capability-stripping payload transition. Do not disable AppArmor or its userns
# restriction, install an unconfined replacement, or run the payload as root.
restriction=/proc/sys/kernel/apparmor_restrict_unprivileged_userns
if [[ -r /sys/module/apparmor/parameters/enabled && -r "$restriction" ]] &&
   grep -q '^Y' /sys/module/apparmor/parameters/enabled &&
   [[ "$(cat "$restriction")" != 0 ]]; then
  profile=/etc/apparmor.d/bwrap-userns-restrict
  if [[ ! -f "$profile" ]]; then
    source_profile=/usr/share/apparmor/extra-profiles/bwrap-userns-restrict
    if [[ ! -f "$source_profile" ]]; then
      echo 'Distribution bwrap AppArmor profile is unavailable; refusing an unconfined fallback' >&2
      exit 1
    fi
    sudo install -m 0644 "$source_profile" "$profile"
  fi
  sudo apparmor_parser -r "$profile"
fi

# Real unprivileged network-namespace setup, before the much longer full suite.
# The trusted no-op uses a read-only filesystem; this is only bootstrap proof.
/usr/bin/bwrap --unshare-user --unshare-pid --unshare-net --unshare-ipc --unshare-uts \
  --cap-drop ALL --ro-bind / / -- /bin/true
echo 'Native bubblewrap bootstrap passed; payload isolation still requires the native suite'
