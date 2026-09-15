#!/bin/sh
# retrieval-mcp installer: fetch a verified prebuilt `retrieval-mcp` binary from GitHub Releases.
#
#   curl -fsSL https://raw.githubusercontent.com/abendrothj/retrieval-mcp/main/install.sh | sh
#
# Environment:
#   RETRIEVAL_MCP_VERSION      tag to install (default: latest release)
#   RETRIEVAL_MCP_INSTALL_DIR  install directory (default: ~/.local/bin)
#   RETRIEVAL_MCP_REGISTER     register with these clients after installing: claude, codex, or
#                              both comma-separated (default: none, the commands are printed)
#   RETRIEVAL_MCP_SCOPE        local (this repository, the default) or user (every project)
#
# No Rust toolchain, no `jq` and no GitHub token required. Unsupported platforms are reported
# instead of silently falling back.
#
# What this script does by default: it copies one binary into one directory and prints the two
# client-registration commands. It does not edit shell startup files, does not write any MCP
# client configuration, does not create anything inside your repositories and does not start
# anything. `RETRIEVAL_MCP_REGISTER` opts into the registration step and runs each client's own
# `mcp add`; nothing here is ever interactive, because a piped installer cannot prompt.
# The server has no config file, no data directory and no index to install.

set -eu

REPO="abendrothj/retrieval-mcp"
VERSION="${RETRIEVAL_MCP_VERSION:-}"
INSTALL_DIR="${RETRIEVAL_MCP_INSTALL_DIR:-$HOME/.local/bin}"
RELEASES="https://github.com/${REPO}/releases"
TARGETS="aarch64-apple-darwin, x86_64-apple-darwin, aarch64-unknown-linux-gnu,
         x86_64-unknown-linux-gnu, aarch64-unknown-linux-musl, x86_64-unknown-linux-musl"

REGISTER="${RETRIEVAL_MCP_REGISTER:-}"
SCOPE="${RETRIEVAL_MCP_SCOPE:-local}"
case "$SCOPE" in
local | user) ;;
*)
	printf 'retrieval-mcp: RETRIEVAL_MCP_SCOPE understands local and user, not: %s\n' "$SCOPE" >&2
	exit 1
	;;
esac

die() {
	printf 'retrieval-mcp: %s\n' "$1" >&2
	exit 1
}

os="$(uname -s)"
arch="$(uname -m)"

case "$os" in
Darwin)
	case "$arch" in
	arm64 | aarch64) target="aarch64-apple-darwin" ;;
	x86_64) target="x86_64-apple-darwin" ;;
	*) die "unsupported macOS architecture: $arch (release targets: $TARGETS)" ;;
	esac
	;;
Linux)
	# Alpine and other musl distributions cannot run a glibc binary at all, and the failure they
	# produce - "No such file or directory" from a file that plainly exists - explains nothing. The
	# loader's own name is the reliable signal; `ldd --version` is the fallback, and on glibc it
	# answers without either matching.
	libc=gnu
	for loader in /lib/ld-musl-*.so.1; do
		[ -e "$loader" ] || continue
		libc=musl
		break
	done
	if [ "$libc" = gnu ] && command -v ldd >/dev/null 2>&1 &&
		ldd --version 2>&1 | head -1 | grep -qi musl; then
		libc=musl
	fi
	case "$arch" in
	aarch64 | arm64) target="aarch64-unknown-linux-${libc}" ;;
	x86_64) target="x86_64-unknown-linux-${libc}" ;;
	*) die "unsupported Linux architecture: $arch (release targets: $TARGETS)" ;;
	esac
	;;
MINGW* | MSYS* | CYGWIN* | Windows_NT)
	die "Windows is not supported: the server's Unix-only paths have never been exercised there, so no Windows binary is published. Release targets: $TARGETS. Under WSL the Linux binary works, or build from source with \`cargo install retrieval-mcp\`"
	;;
*)
	die "unsupported operating system: $os (release targets: $TARGETS)"
	;;
esac

if command -v curl >/dev/null 2>&1; then
	fetch() { curl -fsSL "$1" -o "$2"; }
	# `-I` keeps this a HEAD request: the redirect target is the whole answer, the page is not.
	final_url() { curl -fsSLI -o /dev/null -w '%{url_effective}' "$1"; }
elif command -v wget >/dev/null 2>&1; then
	fetch() { wget -qO "$2" "$1"; }
	final_url() {
		# wget cannot report an effective URL, so the one redirect GitHub serves for
		# `releases/latest` is read off the `Location:` header instead of being followed.
		# wget mentions the target twice - once as the response header and once in its own
		# progress line, which appends ` [following]` - so the first mention wins and that
		# annotation is stripped either way.
		headers="$(wget --spider --server-response --max-redirect=0 "$1" 2>&1 | tr -d '\r')"
		location=
		while IFS= read -r header || [ -n "$header" ]; do
			[ -z "$location" ] || continue
			case "$header" in
			*[Ll]ocation:\ *)
				location="${header#*[Ll]ocation: }"
				location="${location%" [following]"}"
				;;
			esac
		done <<EOF
$headers
EOF
		[ -n "$location" ] || return 1
		printf '%s\n' "$location"
	}
else
	die "need curl or wget to download from ${RELEASES}"
fi

# The asset name embeds the tag, so "latest" cannot be spelled without knowing which tag that is.
# `releases/latest` redirects to `releases/tag/<tag>`, so one HEAD request names the tag: no API
# call, no token, no `jq`, and nothing is downloaded in order to find out what to download.
if [ -n "$VERSION" ]; then
	tag="$VERSION"
else
	latest_url="$(final_url "${RELEASES}/latest")" ||
		die "could not resolve the latest release tag from ${RELEASES}/latest"
	tag="${latest_url##*/}"
	# A version tag is what that final path segment has to be. Anything else means the redirect
	# went somewhere unexpected - no releases yet, a login wall, a rewritten URL - and building
	# an asset name out of it would only turn a clear failure into a confusing 404.
	case "$tag" in
	v[0-9]*.[0-9]*) ;;
	*) die "expected ${RELEASES}/latest to redirect to a release tag, got: $latest_url" ;;
	esac
	case "$tag" in
	*[!0-9A-Za-z.+-]*)
		die "expected ${RELEASES}/latest to redirect to a release tag, got: $latest_url"
		;;
	esac
fi

# With the tag known, the archive name is fully determined; nothing else has to name it.
download="${RELEASES}/download/${tag}"
asset="retrieval-mcp-${tag}-${target}.tar.gz"

tmp="$(mktemp -d)"
stage=
cleanup() {
	rm -rf "$tmp"
	[ -z "$stage" ] || rm -f "$stage"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# Two digest sources, one per release generation: `SHA256SUMS` is the aggregate manifest that
# releases publish from the generation which introduced it, and `<archive>.tar.gz.sha256` is the
# per-archive digest that every release publishes, including v0.1.4 and earlier, which predate the
# manifest. The fallback is not a weaker check: both files are written by the same release job from
# the same archive under the same tag, both are parsed by the same record rules below, and either
# way the digest is compared before anything is extracted. Only which file carries it moves.
sums_url="${download}/SHA256SUMS"
sha_url="${download}/${asset}.sha256"
printf 'retrieval-mcp: checking for %s\n' "$sums_url"
# A missing manifest is a release generation, not an error, so the fetcher's own diagnostic is
# dropped here and only here; every other failure keeps its message.
if fetch "$sums_url" "$tmp/SHA256SUMS" 2>/dev/null; then
	digest_kind=manifest
	digest_url="$sums_url"
	digest_file="$tmp/SHA256SUMS"
else
	digest_kind=single
	digest_url="$sha_url"
	digest_file="$tmp/${asset}.sha256"
	printf 'retrieval-mcp: %s publishes no SHA256SUMS, downloading %s\n' "$tag" "$digest_url"
	fetch "$digest_url" "$digest_file" ||
		die "could not download a checksum for $asset: neither $sums_url nor $sha_url"
fi

# Records are `<64 hex digits><two spaces><name>`, as written by `shasum -a 256`. Anything else in
# either file is a reason to stop rather than to guess.
split_record() {
	record_digest=${1%% *}
	record_name=${1#"$record_digest"}
	record_name=${record_name#  }
	[ "$1" = "${record_digest}  ${record_name}" ] || return 1
	[ "${#record_digest}" -eq 64 ] || return 1
	case "$record_digest" in
	*[!0123456789abcdefABCDEF]*) return 1 ;;
	esac
	[ -n "$record_name" ] || return 1
}

expected=
while IFS= read -r line || [ -n "$line" ]; do
	[ -n "$line" ] || continue
	case "$line" in
	*"  $asset")
		split_record "$line" || die "malformed checksum record for $asset in $digest_url"
		[ "$record_name" = "$asset" ] ||
			die "malformed checksum record for $asset in $digest_url"
		[ -z "$expected" ] || die "duplicate checksum records for $asset in $digest_url"
		expected=$record_digest
		;;
	*"$asset"*)
		die "malformed checksum record for $asset in $digest_url"
		;;
	*)
		# The manifest covers every target, so its other records are expected. The
		# per-archive file has exactly one job and may not name anything else.
		[ "$digest_kind" = manifest ] ||
			die "$digest_url names something other than $asset: $line"
		;;
	esac
done <"$digest_file"
[ -n "$expected" ] || die "$digest_url has no record for $asset"

url="${download}/${asset}"
printf 'retrieval-mcp: downloading %s\n' "$url"
fetch "$url" "$tmp/$asset" || die "download failed: $url"

if [ "$os" = Darwin ]; then
	if command -v shasum >/dev/null 2>&1; then
		actual_output="$(shasum -a 256 "$tmp/$asset")" ||
			die "could not checksum $asset with shasum"
	elif command -v sha256sum >/dev/null 2>&1; then
		actual_output="$(sha256sum "$tmp/$asset")" ||
			die "could not checksum $asset with sha256sum"
	else
		die "need shasum or sha256sum to verify $asset"
	fi
else
	if command -v sha256sum >/dev/null 2>&1; then
		actual_output="$(sha256sum "$tmp/$asset")" ||
			die "could not checksum $asset with sha256sum"
	elif command -v shasum >/dev/null 2>&1; then
		actual_output="$(shasum -a 256 "$tmp/$asset")" ||
			die "could not checksum $asset with shasum"
	else
		die "need sha256sum or shasum to verify $asset"
	fi
fi
actual=${actual_output%% *}
[ "${#actual}" -eq 64 ] || die "checksum tool returned malformed output"
case "$actual" in
*[!0123456789abcdefABCDEF]*) die "checksum tool returned malformed output" ;;
esac
# Nothing is extracted before this line: a tampered archive is never unpacked.
[ "$actual" = "$expected" ] || die "checksum mismatch for $asset"

tar xzf "$tmp/$asset" -C "$tmp" || die "could not extract $asset"

archive_dir="$tmp/${asset%.tar.gz}"
binary="$archive_dir/retrieval-mcp"
[ -f "$binary" ] ||
	die "release archive did not contain ${asset%.tar.gz}/retrieval-mcp"

mkdir -p "$INSTALL_DIR" || die "could not create $INSTALL_DIR"
stage="$(mktemp "$INSTALL_DIR/.retrieval-mcp.XXXXXX")" ||
	die "could not stage retrieval-mcp in $INSTALL_DIR"
cp "$binary" "$stage" || die "could not stage retrieval-mcp"
chmod 755 "$stage" || die "could not make retrieval-mcp executable"

# Run what was downloaded before and after installing it. `--version` is the direct question, but
# it only answers from v0.1.4 onwards: earlier releases parse it as a flag expecting a value and
# exit 1, so a pinned `v0.1.3` would fail here with "failed its version check" while the binary is
# perfectly good. `--help` has printed the same banner since v0.1.0, so it is the fallback probe,
# and either way an archive that cannot execute at all is still refused.
probe() {
	probe_output="$("$1" --version 2>/dev/null)" || probe_output=
	[ -n "$probe_output" ] || probe_output="$("$1" --help 2>/dev/null | head -1)" || probe_output=
	[ -n "$probe_output" ] || return 1
	case "$probe_output" in
	retrieval-mcp\ *) ;;
	*) return 1 ;;
	esac
	printf '%s\n' "$probe_output"
}

probe "$stage" >/dev/null ||
	die "downloaded retrieval-mcp did not run, or reported an unexpected identity"

# Staged in the destination directory, so this rename is atomic: a concurrent run either sees the
# old binary or the new one, and a process already running keeps its open executable.
mv -f "$stage" "$INSTALL_DIR/retrieval-mcp" ||
	die "could not install retrieval-mcp into $INSTALL_DIR"
stage=

installed="$(probe "$INSTALL_DIR/retrieval-mcp")" ||
	die "installed retrieval-mcp did not run, or reported an unexpected identity"
printf 'retrieval-mcp: installed %s to %s\n' "$installed" "$INSTALL_DIR"

case ":${PATH:-}:" in
*":$INSTALL_DIR:"*) ;;
*)
	# shellcheck disable=SC2016 # the trailing $PATH is text for the user to paste, not an expansion
	printf 'retrieval-mcp: %s is not on your PATH. Add it for this shell with:\n\n    export PATH="%s:$PATH"\n\n' \
		"$INSTALL_DIR" "$INSTALL_DIR"
	;;
esac

# Registration is opt-in and never interactive. `curl | sh` makes the script itself this shell's
# stdin, so a prompt would eat its own remaining lines rather than ask anyone anything; an
# installer that registers only when told to, by name, works the same piped, in CI and under an
# agent. It shells out to each client's own CLI rather than editing its configuration file,
# because that file's format belongs to the client.
register_claude() {
	command -v claude >/dev/null 2>&1 ||
		die "RETRIEVAL_MCP_REGISTER names claude, but the claude CLI is not on PATH"
	claude mcp add --transport stdio --scope "$1" retrieval -- \
		"$INSTALL_DIR/retrieval-mcp" --root . ||
		die "claude mcp add failed; register by hand with the command printed above"
	printf 'retrieval-mcp: registered with Claude Code at %s scope\n' "$1"
}

register_codex() {
	command -v codex >/dev/null 2>&1 ||
		die "RETRIEVAL_MCP_REGISTER names codex, but the codex CLI is not on PATH"
	# Codex keeps one server list in ~/.codex/config.toml; it has no project scope to choose.
	codex mcp add retrieval -- "$INSTALL_DIR/retrieval-mcp" --root . ||
		die "codex mcp add failed; register by hand with the command printed above"
	printf 'retrieval-mcp: registered with Codex (user configuration)\n'
}

printf 'retrieval-mcp: register it from a repository you want it to read (--root is fixed at startup, so a local entry is per project):\n\n'
printf '    claude mcp add --transport stdio --scope local retrieval -- retrieval-mcp --root .\n'
printf '    codex mcp add retrieval -- retrieval-mcp --root .\n\n'

if [ -z "$REGISTER" ]; then
	printf 'retrieval-mcp: no configuration file was written and nothing was started. This installer\n'
	printf '               copied one binary into %s; that is all it did.\n' "$INSTALL_DIR"
	printf '               Pass RETRIEVAL_MCP_REGISTER=claude,codex to run those commands for you,\n'
	printf '               and RETRIEVAL_MCP_SCOPE=user for an entry every project sees.\n'
	exit 0
fi

# A local entry names this directory, so refuse to write one from somewhere that is not a
# checkout: a registration rooted at $HOME indexes a home directory, which nobody asked for.
if [ "$SCOPE" = local ] && ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
	die "RETRIEVAL_MCP_SCOPE=local registers the current directory, which is not a git work tree;
       run this from a repository, or pass RETRIEVAL_MCP_SCOPE=user"
fi

# `--root .` is resolved once against the directory the client launches the server in, so one user
# entry still reads whichever project is open. That is what makes a global registration coherent
# for a server whose root is fixed at startup.
printf 'retrieval-mcp: registering %s at %s scope, from %s\n' "$REGISTER" "$SCOPE" "$PWD"
saved_ifs=$IFS
IFS=,
for client in $REGISTER; do
	IFS=$saved_ifs
	case "$client" in
	claude) register_claude "$SCOPE" ;;
	codex) register_codex ;;
	*) die "RETRIEVAL_MCP_REGISTER understands claude and codex, not: $client" ;;
	esac
	IFS=,
done
IFS=$saved_ifs
