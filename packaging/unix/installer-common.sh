# Shared transactional installer. Sourced by the platform-specific entry point.
# Requires Bash 3.2+, ordinary Unix file tools, and an already verified payload.

fail() { echo "$*" >&2; exit 1; }

safe_path() {
  local value="$1" part result="" parts=()
  case "$value" in /*) ;; *) fail "Path must be absolute: $value" ;; esac
  case "$value" in *$'\n'*|*$'\r'*|*$'\t'*) fail "Control character in path" ;; esac
  IFS=/ read -r -a parts <<< "$value"
  for part in "${parts[@]}"; do
    case "$part" in ""|.) continue ;; ..) fail "Parent traversal in path: $value" ;; esac
    result="$result/$part"
    [ ! -L "$result" ] || fail "Redirected path component: $result"
    if [ -e "$result" ] && [ "$result" != "$value" ] && [ ! -d "$result" ]; then
      fail "Non-directory path component: $result"
    fi
    if [ -d "$result" ]; then result="$(CDPATH= cd -- "$result" && pwd -P)"; fi
  done
  printf '%s\n' "${result:-/}"
}

contains_path() { case "$2/" in "$1/"*) return 0 ;; *) return 1 ;; esac; }

owner_record() {
  printf 'gitgo-install-v1\n%s\n%s\n%s\n%s\n%s\n' \
    "$PRODUCT_ID" "$PLATFORM" "$(id -u)" "$INSTALL_ROOT" "$BIN_DIR"
}

owned_install() {
  [ -d "$INSTALL_ROOT" ] && [ ! -L "$INSTALL_ROOT" ] &&
    [ -O "$INSTALL_ROOT" ] &&
    [ -f "$INSTALL_ROOT/.gitgo-install-owner" ] &&
    [ ! -L "$INSTALL_ROOT/.gitgo-install-owner" ] &&
    [ -O "$INSTALL_ROOT/.gitgo-install-owner" ] &&
    [ "$(cat "$INSTALL_ROOT/.gitgo-install-owner")" = "$(owner_record)" ] &&
    [ -f "$INSTALL_ROOT/product.json" ] && [ ! -L "$INSTALL_ROOT/product.json" ] &&
    grep -Eq '"product_id"[[:space:]]*:[[:space:]]*"gitgo-terminal"' "$INSTALL_ROOT/product.json" &&
    grep -Eq "\"platform\"[[:space:]]*:[[:space:]]*\"$PLATFORM\"" "$INSTALL_ROOT/product.json"
}

ensure_dir() {
  local dir="$1"
  [ ! -L "$dir" ] || fail "Redirected directory: $dir"
  if [ -d "$dir" ]; then return; fi
  [ ! -e "$dir" ] || fail "Not a directory: $dir"
  ensure_dir "$(dirname "$dir")"
  mkdir "$dir"
  CREATED_DIRS+=("$dir")
}

install_transaction() {
  HOME="$(safe_path "$HOME")"
  INSTALL_ROOT="$(safe_path "$INSTALL_ROOT")"
  BIN_DIR="$(safe_path "$BIN_DIR")"
  [ ! -e "$SCRIPT_DIR/.gitgo-install-owner" ] && [ ! -L "$SCRIPT_DIR/.gitgo-install-owner" ] ||
    fail "Use an extracted release, not an installed directory"
  [ "$PRODUCT_ID" = gitgo-terminal ] || fail "Unexpected product identity"
  [ "$INSTALL_ROOT" != / ] && [ "$BIN_DIR" != / ] && [ "$BIN_DIR" != "$HOME" ] || fail "Unsafe target"
  contains_path "$INSTALL_ROOT" "$HOME" && fail "Install target contains HOME"
  contains_path "$INSTALL_ROOT" "$SCRIPT_DIR" && fail "Install target contains release source"
  contains_path "$SCRIPT_DIR" "$INSTALL_ROOT" && fail "Install target is inside release source"
  contains_path "$INSTALL_ROOT" "$BIN_DIR" && fail "Command directory is inside installation"
  contains_path "$BIN_DIR" "$INSTALL_ROOT" && fail "Installation is inside command directory"
  INSTALL_PARENT="$(dirname "$INSTALL_ROOT")"
  if [ -e "$INSTALL_ROOT" ]; then
    owned_install || fail "Refusing unknown install directory (ownership marker required): $INSTALL_ROOT"
  fi

  local name profile_kind="" existing_block="" quoted_bin profile_tag=""
  COMMANDS=("$COMMAND")
  for name in ${ALIASES[@]+"${ALIASES[@]}"}; do COMMANDS+=("$name"); done
  for name in "${COMMANDS[@]}"; do
    case "$name" in ""|.|..|*[!A-Za-z0-9._-]*) fail "Unsafe command: $name" ;; esac
    if [ -e "$BIN_DIR/$name" ] || [ -L "$BIN_DIR/$name" ]; then
      [ -L "$BIN_DIR/$name" ] && [ "$(readlink "$BIN_DIR/$name")" = "$INSTALL_ROOT/$name" ] &&
        owned_install || fail "Command conflict: $BIN_DIR/$name"
    fi
    [ -x "$SCRIPT_DIR/$name" ] || fail "Missing command payload: $name"
  done

  PROFILE=""
  if [ "$UPDATE_PATH" -eq 1 ]; then
    case "$(basename "${SHELL:-}")" in
      zsh) PROFILE="$HOME/.zprofile"; profile_kind=posix; profile_tag=zsh ;;
      bash) PROFILE="$HOME/$BASH_PROFILE"; profile_kind=posix; profile_tag=bash ;;
      fish) PROFILE="$HOME/.config/fish/conf.d/gitgo.fish"; profile_kind=fish; profile_tag=fish ;;
      *) echo "Add to PATH manually: $BIN_DIR" >&2 ;;
    esac
  fi
  EXPECTED_BLOCK=""
  if [ -n "$PROFILE" ]; then
    PROFILE="$(safe_path "$PROFILE")"
    contains_path "$INSTALL_ROOT" "$PROFILE" && fail "Profile is inside installation"
    [ ! -e "$PROFILE" ] || [ -f "$PROFILE" ] || fail "Profile is not a regular file"
    quoted_bin="$(printf '%q' "$BIN_DIR")"
    if [ "$profile_kind" = posix ]; then
      EXPECTED_BLOCK="$(printf '%s\nexport PATH=%s:"$PATH"\n%s' "$START_MARKER" "$quoted_bin" "$END_MARKER")"
    else
      # Single-quoted fish string, not Bash's %q syntax.
      quoted_bin="$(printf '%s' "$BIN_DIR" | sed "s/\\\\/\\\\\\\\/g; s/'/\\\\'/g")"
      EXPECTED_BLOCK="$(printf "%s\nfish_add_path --path '%s'\n%s" "$START_MARKER" "$quoted_bin" "$END_MARKER")"
    fi
    if [ -f "$PROFILE" ]; then
      existing_block="$(awk -v start="$START_MARKER" -v end="$END_MARKER" \
        '$0 == start {block=1} block {print} $0 == end {block=0}' "$PROFILE")"
      if grep -Fq "$START_MARKER" "$PROFILE" || grep -Fq "$END_MARKER" "$PROFILE"; then
        [ "$existing_block" = "$EXPECTED_BLOCK" ] || fail "Conflicting managed PATH block"
      elif [ "$profile_kind" = fish ]; then
        fail "Refusing unknown fish configuration: $PROFILE"
      fi
    fi
  fi

  CREATED_DIRS=(); CREATED_LINKS=(); TXN=""; LOCK=""; PROFILE_TEMP=""
  PROFILE_TOUCHED=0; PROFILE_EXISTED=0; ROOT_PUBLISHING=0; COMMITTED=0
  rollback_install() {
    local status=$? item recovery_failed=0 i
    trap - EXIT INT TERM
    set +e
    if [ "$COMMITTED" -eq 0 ]; then
      if [ "$PROFILE_TOUCHED" -eq 1 ]; then
        if [ "$PROFILE_EXISTED" -eq 1 ]; then
          mv "$TXN/profile.previous" "$PROFILE" || recovery_failed=1
        else
          rm -f "$PROFILE" || recovery_failed=1
        fi
      fi
      for item in ${CREATED_LINKS[@]+"${CREATED_LINKS[@]}"}; do
        if [ -L "$item" ] && [ "$(readlink "$item")" = "$INSTALL_ROOT/$(basename "$item")" ]; then
          rm "$item" || recovery_failed=1
        fi
      done
      if [ "$ROOT_PUBLISHING" -eq 1 ] && [ ! -e "$TXN/payload" ] && [ -e "$INSTALL_ROOT" ]; then
        if owned_install; then rm -rf "$INSTALL_ROOT" || recovery_failed=1
        else echo "Target ownership changed; retaining recovery files" >&2; recovery_failed=1; fi
      fi
      if [ -n "$TXN" ] && [ -d "$TXN/previous" ]; then
        if [ ! -e "$INSTALL_ROOT" ]; then mv "$TXN/previous" "$INSTALL_ROOT" || recovery_failed=1
        else recovery_failed=1; fi
      fi
    fi
    [ -z "$PROFILE_TEMP" ] || rm -f "$PROFILE_TEMP"
    if [ -n "$TXN" ]; then
      if [ "$recovery_failed" -eq 0 ]; then rm -rf "$TXN"
      else echo "Recovery required; retained transaction: $TXN" >&2; status=1; fi
    fi
    [ -z "$LOCK" ] || rmdir "$LOCK"
    if [ "$COMMITTED" -eq 0 ]; then
      for ((i=${#CREATED_DIRS[@]}-1; i>=0; i--)); do rmdir "${CREATED_DIRS[$i]}" 2>/dev/null; done
      [ "$status" -ne 0 ] || status=1
      echo "Installation failed; rollback attempted (exit $status)" >&2
    fi
    exit "$status"
  }
  trap rollback_install EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  ensure_dir "$INSTALL_PARENT"
  # Never remove a pre-existing lock or PID-derived temporary directory.
  local lock_path="$INSTALL_PARENT/.gitgo-install.lock"
  mkdir "$lock_path" || fail "Installation already locked; inspect $lock_path"
  LOCK="$lock_path"
  TXN="$(mktemp -d "$INSTALL_PARENT/.gitgo-transaction.XXXXXXXX")"
  ensure_dir "$BIN_DIR"
  mkdir "$TXN/payload"
  cp -pR "$SCRIPT_DIR/." "$TXN/payload/"
  owner_record > "$TXN/payload/.gitgo-install-owner"
  # Preserve ownership of PATH blocks written by earlier owned upgrades.
  for name in bash zsh fish; do
    if [ -f "$INSTALL_ROOT/.gitgo-path-$name" ] && [ ! -L "$INSTALL_ROOT/.gitgo-path-$name" ]; then
      cp -p "$INSTALL_ROOT/.gitgo-path-$name" "$TXN/payload/.gitgo-path-$name"
    fi
  done
  if [ -n "$PROFILE" ]; then
    printf 'gitgo-managed-path-v1\n%s\n%s\n' "$PROFILE" "$EXPECTED_BLOCK" > "$TXN/payload/.gitgo-path-$profile_tag"
    ensure_dir "$(dirname "$PROFILE")"
    PROFILE_TEMP="$(mktemp "$(dirname "$PROFILE")/.gitgo-profile.XXXXXXXX")"
    if [ -f "$PROFILE" ]; then
      PROFILE_EXISTED=1
      cp -p "$PROFILE" "$TXN/profile.previous"
      cp -p "$PROFILE" "$PROFILE_TEMP"
    fi
    if [ -z "$existing_block" ]; then printf '\n%s\n' "$EXPECTED_BLOCK" >> "$PROFILE_TEMP"; fi
  fi

  # Recheck targets just before commit. This is not an OS isolation boundary.
  [ "$(safe_path "$INSTALL_ROOT")" = "$INSTALL_ROOT" ] || fail "Target changed"
  [ "$(safe_path "$BIN_DIR")" = "$BIN_DIR" ] || fail "Command directory changed"
  if [ -e "$INSTALL_ROOT" ]; then
    owned_install || fail "Installation ownership changed"
    mv "$INSTALL_ROOT" "$TXN/previous"
  fi
  ROOT_PUBLISHING=1
  mv "$TXN/payload" "$INSTALL_ROOT"
  for name in "${COMMANDS[@]}"; do
    if [ ! -L "$BIN_DIR/$name" ]; then
      # Record the intended owned link before ln so interrupts cannot orphan it.
      CREATED_LINKS+=("$BIN_DIR/$name")
      ln -s "$INSTALL_ROOT/$name" "$BIN_DIR/$name"
    fi
    [ "$(readlink "$BIN_DIR/$name")" = "$INSTALL_ROOT/$name" ] || fail "Command changed"
  done
  if [ -n "$PROFILE" ]; then
    [ "$(safe_path "$PROFILE")" = "$PROFILE" ] || fail "Profile redirected"
    if [ "$PROFILE_EXISTED" -eq 1 ]; then cmp -s "$PROFILE" "$TXN/profile.previous" || fail "Profile changed concurrently"
    else [ ! -e "$PROFILE" ] || fail "Profile appeared concurrently"; fi
    PROFILE_TOUCHED=1
    mv "$PROFILE_TEMP" "$PROFILE"
    PROFILE_TEMP=""
  fi
  COMMITTED=1
  echo "Gitgo installed at: $INSTALL_ROOT"
  echo "Command: $BIN_DIR/$COMMAND"
}

remove_owned_path_blocks() {
  local tag record header profile expected observed starts ends snapshot temporary lock
  for tag in bash zsh fish; do
    record="$INSTALL_ROOT/.gitgo-path-$tag"
    [ -e "$record" ] || [ -L "$record" ] || continue
    if [ ! -f "$record" ] || [ -L "$record" ] || [ ! -O "$record" ]; then
      echo "PATH ownership record changed; preserving user profile ($tag)" >&2
      continue
    fi
    header="$(sed -n '1p' "$record")"
    profile="$(sed -n '2p' "$record")"
    expected="$(sed '1,2d' "$record")"
    case "$tag:$profile" in
      "bash:$HOME/.profile"|"bash:$HOME/.bash_profile"|"zsh:$HOME/.zprofile"|"fish:$HOME/.config/fish/conf.d/gitgo.fish") ;;
      *) echo "Unknown managed profile; preserving it ($tag)" >&2; continue ;;
    esac
    if [ "$header" != gitgo-managed-path-v1 ] || [ -L "$profile" ]; then
      echo "Profile ownership changed; preserving it: $profile" >&2
      continue
    fi
    [ -f "$profile" ] || continue
    # No managed marker means no write, chmod, rename or symlink replacement.
    starts="$(grep -Fxc "$START_MARKER" "$profile" || true)"
    ends="$(grep -Fxc "$END_MARKER" "$profile" || true)"
    [ "$starts:$ends" != 0:0 ] || continue
    observed="$(awk -v start="$START_MARKER" -v end="$END_MARKER" '$0 == start {block=1} block {print} $0 == end {block=0}' "$profile")"
    if [ "$starts:$ends" != 1:1 ] || [ "$observed" != "$expected" ]; then
      echo "Managed PATH block changed; preserving profile: $profile" >&2
      continue
    fi
    # Parent redirects are refused before allocating files beside the profile.
    [ "$(safe_path "$profile")" = "$profile" ] || fail "Profile redirected"
    lock="$profile.gitgo-path.lock"
    if ! mkdir "$lock"; then
      echo "Profile is locked; preserving it: $profile" >&2
      continue
    fi
    snapshot="$(mktemp "$(dirname "$profile")/.gitgo-path-original.XXXXXXXX")"
    temporary="$(mktemp "$(dirname "$profile")/.gitgo-path-updated.XXXXXXXX")"
    if ! cp -p "$profile" "$snapshot" || ! cp -p "$profile" "$temporary" ||
       ! awk -v start="$START_MARKER" -v end="$END_MARKER" '$0 == start {skip=1;next} $0 == end {skip=0;next} !skip {print}' "$snapshot" > "$temporary"; then
      rm -f "$snapshot" "$temporary"; rmdir "$lock"
      fail "Cannot safely update managed PATH profile"
    fi
    if [ -L "$profile" ] || ! cmp -s "$profile" "$snapshot"; then
      echo "Profile changed concurrently; preserving it: $profile" >&2
      rm -f "$snapshot" "$temporary"; rmdir "$lock"
      continue
    fi
    if ! mv "$temporary" "$profile"; then
      rm -f "$snapshot" "$temporary"; rmdir "$lock"
      fail "Cannot commit managed PATH removal"
    fi
    rm -f "$snapshot"; rmdir "$lock"
  done
}
