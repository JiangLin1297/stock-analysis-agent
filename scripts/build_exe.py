#!/usr/bin/env python3
"""
StockMind Packaging Script — PyInstaller-based, one-dir build with desktop shortcut.

Usage:
    python build_exe.py          # Full build
    python build_exe.py --clean  # Clean + rebuild

Output:
    dist/StockMind/StockMind.exe
    Desktop/StockMind.lnk (shortcut)
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
ENTRY_SCRIPT = os.path.join(PROJECT_DIR, "desktop_app.py")


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
        (10, 6), (11, 6), (12, 6), (13, 6),
        (10, 7), (10, 8), (10, 9),
        (10, 10), (11, 10), (12, 10), (13, 10),
        (13, 11), (13, 12), (13, 13),
        (10, 14), (11, 14), (12, 14), (13, 14),
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
    """Collect data files and Python modules to bundle."""
    data_files = []

    # Data files
    for fname in ['portfolio.json', 'config.json']:
        fpath = os.path.join(PROJECT_DIR, fname)
        if os.path.exists(fpath):
            data_files.append((fpath, '.'))

    # Adapted params files
    for fname in os.listdir(PROJECT_DIR):
        if fname.endswith("_adapted_params.json"):
            data_files.append((os.path.join(PROJECT_DIR, fname), '.'))

    # Package directories (source .py files needed at runtime)
    for pkg in ['agents', 'data', 'analysis', 'backtest', 'evolution',
                'portfolio', 'mail', 'ui', 'utils']:
        pkg_dir = os.path.join(PROJECT_DIR, pkg)
        if os.path.isdir(pkg_dir):
            data_files.append((pkg_dir, pkg))

    # Scripts directory (for runtime hooks)
    scripts_dir = os.path.join(PROJECT_DIR, 'scripts')
    if os.path.isdir(scripts_dir):
        for fname in os.listdir(scripts_dir):
            if fname.endswith('.py'):
                data_files.append(
                    (os.path.join(scripts_dir, fname), 'scripts'))

    return data_files


def get_hidden_imports():
    """Collect all project modules and required third-party packages as hidden imports."""
    imports = [
        'PySide6', 'PySide6.QtWidgets', 'PySide6.QtCore', 'PySide6.QtGui',
        'PySide6.QtNetwork',
        'numpy', 'pandas', 'urllib3', 'certifi',
        'pythoncom', 'win32com', 'win32com.client',
    ]
    for pkg in ['agents', 'data', 'analysis', 'backtest', 'evolution',
                'portfolio', 'mail', 'ui', 'utils']:
        pkg_dir = os.path.join(PROJECT_DIR, pkg)
        if os.path.isdir(pkg_dir):
            for fname in os.listdir(pkg_dir):
                if fname.endswith('.py') and fname != '__init__.py':
                    mod_name = f"{pkg}.{fname[:-3]}"
                    if mod_name not in imports:
                        imports.append(mod_name)
    return imports


def create_desktop_shortcut():
    """Create StockMind.lnk on the user's Desktop using PowerShell."""
    dist_exe = os.path.join(PROJECT_DIR, "dist", "StockMind", "StockMind.exe")
    if not os.path.exists(dist_exe):
        print("  [WARN] EXE not found, skipping shortcut creation.")
        return False

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    shortcut_path = os.path.join(desktop, "StockMind.lnk")

    ps_script = f'''
$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath = "{dist_exe}"
$Shortcut.WorkingDirectory = "{os.path.dirname(dist_exe)}"
$Shortcut.IconLocation = "{dist_exe}"
$Shortcut.Description = "StockMind - 多Agent深度股析系统"
$Shortcut.Save()
Write-Host "Shortcut created: {shortcut_path}"
'''

    try:
        result = subprocess.run(
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-Command', ps_script],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            print(f"  [OK] Desktop shortcut: {shortcut_path}")
            return True
        else:
            print(f"  [WARN] Shortcut creation failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"  [WARN] Shortcut creation error: {e}")
        return False


def build():
    """Run PyInstaller packaging."""
    print("=" * 70)
    print("  StockMind Packaging Tool")
    print("=" * 70)

    # Verify entry script exists
    if not os.path.exists(ENTRY_SCRIPT):
        print(f"\n  [FAIL] Entry script not found: {ENTRY_SCRIPT}")
        print(f"  Please ensure desktop_app.py exists in the project root.")
        return False

    print("\n  [1/5] Preparing icon...")
    generate_ico()

    print("\n  [2/5] Collecting data files...")
    data_files = get_data_files()
    for src, dst in data_files:
        print(f"    -> {os.path.basename(src) if not os.path.isdir(src) else src + '/'}")

    print("\n  [3/5] Running PyInstaller...")

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
    cmd.append('--collect-all=akshare')
    cmd.append('--exclude-module=matplotlib')
    cmd.append('--exclude-module=scipy')
    cmd.append('--exclude-module=PIL')
    cmd.append('--exclude-module=tkinter')
    cmd.append(ENTRY_SCRIPT)

    print(f"  Entry: {ENTRY_SCRIPT}")
    print(f"  Running PyInstaller (this may take 2-5 minutes)...")
    result = subprocess.run(cmd, cwd=PROJECT_DIR, capture_output=False)

    if result.returncode != 0:
        print("\n  [FAIL] PyInstaller build failed!")
        return False

    print("\n  [4/5] Verifying output...")
    dist_dir = os.path.join(PROJECT_DIR, "dist", "StockMind")
    exe_path = os.path.join(dist_dir, "StockMind.exe")
    if os.path.exists(exe_path):
        size_mb = os.path.getsize(exe_path) / (1024 * 1024)
        print(f"  [OK] Build successful!")
        print(f"  [OK] Output dir: {dist_dir}")
        print(f"  [OK] EXE: {exe_path} ({size_mb:.1f} MB)")
    else:
        print(f"  [FAIL] Output not found: {exe_path}")
        return False

    print("\n  [5/5] Creating desktop shortcut...")
    create_desktop_shortcut()

    print(f"")
    print(f"  ========================================")
    print(f"  Build Complete!")
    print(f"  ========================================")
    print(f"  桌面快捷方式: StockMind.lnk")
    print(f"  EXE 位置: {exe_path}")
    print(f"")
    print(f"  首次使用:")
    print(f"  1. 双击桌面 StockMind 快捷方式启动")
    print(f"  2. 进入「设置」页面 -> 填写 DeepSeek API Key")
    print(f"  3. 进入「深度分析」页面 -> 输入股票代码 -> 开始分析")
    print(f"")
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='StockMind Packaging Script')
    parser.add_argument('--clean', action='store_true', help='Clean old builds first')
    args = parser.parse_args()

    os.chdir(PROJECT_DIR)
    if args.clean:
        clean_dist()
    success = build()
    sys.exit(0 if success else 1)
