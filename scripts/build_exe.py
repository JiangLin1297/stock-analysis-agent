#!/usr/bin/env python3
"""
StockMind Packaging Script — PyInstaller-based, no-console desktop app.

Usage:
    python build_exe.py          # Full build
    python build_exe.py --clean  # Clean + rebuild

Output:
    dist/StockMind/StockMind.exe (--onedir, recommended for faster startup)
"""

import sys
import os
import shutil
import subprocess
import struct
import argparse

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_PATH = os.path.join(PROJECT_DIR, "assets", "stock.ico")
SPEC_FILE = os.path.join(PROJECT_DIR, "StockMind.spec")


def generate_ico():
    """Generate a default 32x32 ICO file (blue-purple gradient with 'S' letter)."""
    if os.path.exists(ICON_PATH):
        print(f"  [INFO] Icon already exists: {ICON_PATH}")
        return ICON_PATH

    width, height = 32, 32
    pixels = []
    for y in range(height):
        for x in range(width):
            r = int(30 + (x / width) * 80)
            g = int(60 + (y / height) * 60)
            b = int(160 - (x / width) * 40 + (y / height) * 40)
            a = 255
            pixels.append((b, g, r, a))

    # Draw "S" letter in white
    s_coords = [
        (10,6),(11,6),(12,6),(13,6),
        (10,7),(10,8),(10,9),
        (10,10),(11,10),(12,10),(13,10),
        (13,11),(13,12),(13,13),
        (10,14),(11,14),(12,14),(13,14),
    ]
    for cx, cy in s_coords:
        if 0 <= cx < width and 0 <= cy < height:
            idx = cy * width + cx
            pixels[idx] = (255, 255, 255, 255)

    # Build BMP data (bottom-up)
    bmp_data = b''
    for y in range(height - 1, -1, -1):
        for x in range(width):
            b, g, r, a = pixels[y * width + x]
            bmp_data += struct.pack('BBBB', b, g, r, a)

    bmp_header = struct.pack(
        '<IiiHHIIiiII',
        40, width, height * 2, 1, 32, 0,
        len(bmp_data), 0, 0, 0, 0,
    )
    and_mask = b'\x00' * ((width * height) // 8)

    ico_header = struct.pack('<HHH', 0, 1, 1)
    entry = struct.pack(
        '<BBBBHHII',
        width, height, 0, 0, 1, 32,
        len(bmp_header) + len(bmp_data) + len(and_mask),
        22,
    )

    with open(ICON_PATH, 'wb') as f:
        f.write(ico_header + entry + bmp_header + bmp_data + and_mask)

    print(f"  [OK] Generated default icon: {ICON_PATH}")
    return ICON_PATH


def clean_dist():
    """Remove old build artifacts."""
    for d in ['dist', 'build']:
        dpath = os.path.join(PROJECT_DIR, d)
        if os.path.exists(dpath):
            shutil.rmtree(dpath)
            print(f"  [OK] Removed: {d}")
    if os.path.exists(SPEC_FILE):
        os.remove(SPEC_FILE)
        print(f"  [OK] Removed: {SPEC_FILE}")


def get_data_files():
    """Collect data files to bundle."""
    data_files = []
    pf_path = os.path.join(PROJECT_DIR, "portfolio.json")
    if os.path.exists(pf_path):
        data_files.append((pf_path, '.'))
    cfg_path = os.path.join(PROJECT_DIR, "config.json")
    if os.path.exists(cfg_path):
        data_files.append((cfg_path, '.'))
    for fname in os.listdir(PROJECT_DIR):
        if fname.endswith("_adapted_params.json"):
            data_files.append((os.path.join(PROJECT_DIR, fname), '.'))
    return data_files


def get_hidden_imports():
    """Collect all project modules as hidden imports."""
    imports = [
        'PySide6', 'PySide6.QtWidgets', 'PySide6.QtCore', 'PySide6.QtGui',
        'numpy', 'pandas', 'urllib3', 'certifi',
    ]
    import_paths = [
        'agents', 'data', 'analysis', 'backtest', 'evolution',
        'portfolio', 'mail', 'ui', 'utils',
    ]
    for pkg in import_paths:
        pkg_dir = os.path.join(PROJECT_DIR, pkg)
        if os.path.isdir(pkg_dir):
            for fname in os.listdir(pkg_dir):
                if fname.endswith('.py') and fname != '__init__.py':
                    mod_name = f"{pkg}.{fname[:-3]}"
                    if mod_name not in imports:
                        imports.append(mod_name)
    return imports


def build():
    """Run PyInstaller packaging."""
    print("=" * 70)
    print("  StockMind Packaging Tool")
    print("=" * 70)

    print("\n  [1/4] Preparing icon...")
    generate_ico()

    print("\n  [2/4] Collecting data files...")
    data_files = get_data_files()
    for src, dst in data_files:
        print(f"    -> {os.path.basename(src)}")

    print("\n  [3/4] Running PyInstaller...")
    entry_script = os.path.join(PROJECT_DIR, "ui", "app.py")

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconsole',
        '--onedir',
        '--name=StockMind',
        f'--icon={ICON_PATH}',
        '--clean',
        '--noconfirm',
    ]

    for imp in get_hidden_imports():
        cmd.append(f'--hidden-import={imp}')

    for src, dst in data_files:
        cmd.append(f'--add-data={src}{os.pathsep}{dst}')

    cmd.append('--collect-all=PySide6')
    cmd.append('--collect-all=shiboken6')
    cmd.append('--exclude-module=matplotlib')
    cmd.append('--exclude-module=scipy')
    cmd.append('--exclude-module=PIL')
    cmd.append('--exclude-module=tkinter')
    cmd.append(entry_script)

    print(f"  Running PyInstaller (this may take 2-5 minutes)...")
    result = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=False)

    if result.returncode != 0:
        print("\n  [FAIL] PyInstaller build failed!")
        return False

    print("\n  [4/4] Verifying output...")
    dist_dir = os.path.join(PROJECT_DIR, "dist", "StockMind")
    exe_path = os.path.join(dist_dir, "StockMind.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"  [OK] Build successful!")
        print(f"  [OK] Output dir: {dist_dir}")
        print(f"  [OK] EXE: {exe_path} ({size_mb:.1f} MB)")
        print(f"")
        print(f"  User operation flow:")
        print(f"  1. Double-click: {exe_path}")
        print(f"  2. Main window opens with left nav: Overview / Deep Analysis / Screening / Evolution / Settings")
        print(f"  3. 'Deep Analysis' -> enter stock code (e.g. 000001) -> check 'Adaptive Params' -> 'Start Analysis'")
        print(f"  4. 'Evolution' -> 'Backtest Lab' -> enter code -> 'Start Backtest+Evolution'")
        print(f"  5. 'Evolution' -> 'Adaptive Migration' -> enter target code -> 'Extract Genes & Adapt'")
        print(f"  6. 'Evolution' -> 'Gene Library' -> view all saved adaptive strategies")
        return True
    else:
        print(f"  [FAIL] Output not found: {exe_path}")
        return False


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='StockMind Packaging Script')
    parser.add_argument('--clean', action='store_true', help='Clean old builds first')
    args = parser.parse_args()

    os.chdir(PROJECT_DIR)
    if args.clean:
        clean_dist()
    success = build()
    sys.exit(0 if success else 1)
