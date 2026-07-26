# Maintainer: Your Name <you@example.com>
pkgname=crosshair-overlay
pkgver=0.1.0
pkgrel=1
pkgdesc="Native Wayland crosshair overlay for gaming, with a graphical settings tool"
arch=('any')
url="https://github.com/YOURUSER/crosshair-overlay"
license=('MIT')
depends=(
    'python'
    'python-gobject'
    'gtk4'
    'gtk4-layer-shell'
    'python-cairo'
    'gdk-pixbuf2'
)

# --- LOCAL TEST MODE ---------------------------------------------------
# While the four .py files, the .desktop file, and the .svg icon all sit
# next to this PKGBUILD, `source` just lists their filenames and
# sha256sums is 'SKIP' for everything, so `makepkg` copies them straight
# from this directory instead of downloading anything. This is only for
# testing on your own machine.
#
# Once the project has a real GitHub repo with a tagged release, swap
# this whole `source=`/`sha256sums=` pair for a URL-based one, e.g.:
#
#   source=("$pkgname-$pkgver.tar.gz::https://github.com/YOURUSER/crosshair-overlay/archive/refs/tags/v$pkgver.tar.gz")
#   sha256sums=('...')   # fill in with `updpkgsums` after adding the URL
#
# and the paths inside package() below will need the extracted release
# folder name prepended (usually "$pkgname-$pkgver/filename.py") — ask me
# to update this once you've got that repo tagged and I'll adjust it to
# match your actual folder layout.
source=(
    "crosshaird.py"
    "crosshair-gui.py"
    "crosshairctl.py"
    "crosshair_common.py"
    "crosshair-overlay.desktop"
    "crosshair-overlay.svg"
)
sha256sums=('SKIP'
            'SKIP'
            'SKIP'
            'SKIP'
            'SKIP'
            'SKIP')
# -------------------------------------------------------------------------

package() {
    local libdir="$pkgdir/usr/lib/crosshair-overlay"

    # The actual Python source lives under /usr/lib, not /usr/bin --
    # crosshair_common.py needs to sit next to the other three scripts
    # so Python's "script's own directory goes on sys.path" behavior
    # finds it, and /usr/lib keeps that implementation detail out of
    # the user's PATH.
    install -Dm644 "$srcdir/crosshair_common.py" "$libdir/crosshair_common.py"
    install -Dm644 "$srcdir/crosshaird.py"        "$libdir/crosshaird.py"
    install -Dm644 "$srcdir/crosshair-gui.py"     "$libdir/crosshair-gui.py"
    install -Dm644 "$srcdir/crosshairctl.py"      "$libdir/crosshairctl.py"

    install -Dm644 "$srcdir/crosshair-overlay.desktop" \
        "$pkgdir/usr/share/applications/crosshair-overlay.desktop"
    install -Dm644 "$srcdir/crosshair-overlay.svg" \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/crosshair-overlay.svg"

    # Thin wrapper executables on PATH. Each one execs the real .py file
    # by its full /usr/lib path (not a symlink) so that when Python sets
    # sys.path[0], it resolves to /usr/lib/crosshair-overlay -- exactly
    # where crosshair_common.py lives. A symlink in /usr/bin pointing at
    # the same file would NOT reliably give the same result, since the
    # interpreter sees the invoked path, not always the symlink target.
    install -d "$pkgdir/usr/bin"
    for name in crosshaird crosshair-gui crosshairctl; do
        cat > "$pkgdir/usr/bin/$name" <<EOF
#!/bin/sh
exec /usr/bin/python3 "/usr/lib/crosshair-overlay/$name.py" "\$@"
EOF
        chmod 755 "$pkgdir/usr/bin/$name"
    done
}
