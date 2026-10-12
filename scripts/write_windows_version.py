"""Generate PyInstaller PE metadata from the validated product version."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.release_artifacts import ROOT, validate_versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    product = validate_versions(ROOT)
    numbers = tuple(int(n) for n in product['windows_version'].split('.'))
    strings = {'FileVersion': product['windows_version'], 'ProductVersion': product['version'],
               'ProductName': product['display_name'], 'FileDescription': 'Gitgo Native Host',
               'OriginalFilename': 'gitgo-host.exe'}
    entries = ','.join(f'StringStruct({name!r},{value!r})' for name, value in strings.items())
    text = (f'VSVersionInfo(ffi=FixedFileInfo(filevers={numbers!r},prodvers={numbers!r},'
            f'mask=0x3f,flags=0x2,OS=0x40004,fileType=0x1,subtype=0x0,date=(0,0)),'
            f'kids=[StringFileInfo([StringTable("040904B0",[{entries}])]),'
            'VarFileInfo([VarStruct("Translation",[1033,1200])])])\n')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding='utf-8')


if __name__ == '__main__':
    main()
