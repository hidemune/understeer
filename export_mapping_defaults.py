#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, re, sys, csv, argparse, fcntl, array, struct
try:
    import evdev
    from evdev import ecodes
except Exception:
    print("[!] python-evdev is required. Install with: sudo pip3 install evdev", file=sys.stderr)
    sys.exit(1)

# 仮想デバイス側の順序（ブロック順）
VIRTUAL_AXES_ORDER = [
    "ABS_X","ABS_Y","ABS_Z","ABS_RX","ABS_RY","ABS_RZ",
    "ABS_THROTTLE","ABS_RUDDER","ABS_HAT0X","ABS_HAT0Y"
]
VIRTUAL_BUTTONS_ORDER = [
    "BTN_TRIGGER","BTN_THUMB","BTN_THUMB2","BTN_TOP","BTN_TOP2","BTN_PINKIE",
    "BTN_BASE","BTN_BASE2","BTN_BASE3","BTN_BASE4","BTN_BASE5","BTN_BASE6",
    "BTN_0","BTN_1","BTN_2","BTN_3","BTN_4","BTN_5","BTN_6","BTN_7","BTN_8","BTN_9",
    "BTN_DEAD"
]

def _code_of(entry):
    """
    entry を整数コードに正規化する:
      - 例: 0, 304 → そのまま
      - 例: ('ABS_X', AbsInfo) / (0, AbsInfo) → 先頭要素
      - 例: 'ABS_X' / 'BTN_A' → ecodes.ecodes から引く
    """
    # タプル/リストなら先頭をとる
    if isinstance(entry, (tuple, list)):
        entry = entry[0]
    # すでに数値ならそのまま
    if isinstance(entry, int):
        return entry
    # 名前文字列なら ecodes から引く（無ければ ValueError）
    if isinstance(entry, str):
        # 'ABS_X' 'BTN_A' 'KEY_1' などを int に
        if entry in ecodes.ecodes:
            return int(ecodes.ecodes[entry])
        # 'ABS_0' のような文字列数字も一応対応
        try:
            return int(entry)
        except Exception:
            pass
    # どれでもなければ失敗
    raise TypeError(f"unsupported capability entry type: {type(entry)} ({entry!r})")

def write_header(f, kind: str):
    if kind == "axes":
        f.write("# VIRTUAL_AXES_ORDER: " + ", ".join(VIRTUAL_AXES_ORDER) + "\n")
    else:
        f.write("# VIRTUAL_BUTTONS_ORDER: " + ", ".join(VIRTUAL_BUTTONS_ORDER) + "\n")
    f.write("# 並び替えガイド: 上から順に割当 / 空行=グループ区切り（同一仮想へ合流）\n")
    # ▼ 末尾に js_index_in_js（KEYならボタン番号 / ABSなら軸番号）を追加
    f.write("# 列: js_index\tdevice_name\tsrc_tag\tsrc_type\tsrc_code_name\tsrc_code\tdefault_virtual\tjs_index_in_js\n")

def get_js_index_for_event(event_path: str):
    try:
        # /sys/class/input/eventX/device の実体（…/input/inputNNN）
        dev = os.path.realpath(f"/sys/class/input/{os.path.basename(event_path)}/device")
        parent = os.path.dirname(dev)  # …/input
        # 兄弟に js* があればそれが一番確度高い
        for name in os.listdir(parent):
            if name.startswith("js"):
                jsdev = os.path.join(parent, name)
                # さらに念押しで device 実体を比較
                if os.path.realpath(os.path.join(jsdev, "device")) == dev:
                    m = re.search(r"js(\d+)$", name)
                    if m: return int(m.group(1))
        # フォールバック：/sys/class/input/js*/device を総当り
        for js in os.listdir("/sys/class/input"):
            if not js.startswith("js"): continue
            jsdev = os.path.join("/sys/class/input", js, "device")
            if os.path.realpath(jsdev) == dev:
                m = re.search(r"js(\d+)$", js)
                if m: return int(m.group(1))
    except Exception:
        pass
    return -1  # 見つからない


def tag_from_name(name: str) -> str:
    nml = (name or "").lower()
    if "wheel" in nml or "racing" in nml or "ffb" in nml or "steering" in nml:
        return "wheel"
    if "shift" in nml or "shifter" in nml:
        return "shift"
    return "src"

def resolve_abs_name(code: int) -> str:
    try:
        return ecodes.bytype(ecodes.EV_ABS, int(code))
    except Exception:
        return f"ABS_{code}"

def resolve_key_name(code: int) -> str:
    try:
        return ecodes.bytype(ecodes.EV_KEY, int(code))
    except Exception:
        return f"KEY_{code}"

# ---------- ここから /dev/input/jsN 由来の “JsTest の番号” を取り出す ----------
# linux/joystick.h より（ioctl 番号）
JSIOCGAXES     = 0x80016a11  # _IOR('j', 0x11, __u8)
JSIOCGBUTTONS  = 0x80016a12  # _IOR('j', 0x12, __u8)
JSIOCGAXMAP    = 0x80406a32  # _IOR('j', 0x32, __u8[ABS_CNT])  仮に64バイト確保
JSIOCGBTNMAP   = 0x80406a34  # _IOR('j', 0x34, __u16[KEY_MAX - BTN_MISC + 1]) 仮に256 * 2

def _open_js_fd(js_index: int):
    if js_index is None or js_index < 0:
        return None
    path = f"/dev/input/js{js_index}"
    try:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except Exception:
        return None

def _read_js_counts(fd):
    # 軸・ボタン数（__u8）
    buf = array.array('B', [0])
    try:
        fcntl.ioctl(fd, JSIOCGAXES, buf, True); axes = int(buf[0])
    except Exception:
        axes = 0
    buf = array.array('B', [0])
    try:
        fcntl.ioctl(fd, JSIOCGBUTTONS, buf, True); btns = int(buf[0])
    except Exception:
        btns = 0
    return axes, btns

def _read_js_maps(fd):
    # AXMAP: __u8[ABS_MAX+1] 程度。余裕を見て 64 バイト確保
    axmap_buf = array.array('B', [0]*64)
    try:
        fcntl.ioctl(fd, JSIOCGAXMAP, axmap_buf, True)
    except Exception:
        pass
    # BTNMAP: __u16[...]。余裕を見て 256 * 2 バイト確保
    btnmap_buf = bytearray(512)
    try:
        fcntl.ioctl(fd, JSIOCGBTNMAP, btnmap_buf, True)
    except Exception:
        pass
    # __u16 配列に解釈
    btnmap = list(struct.unpack_from("<" + "H"*(len(btnmap_buf)//2), btnmap_buf))
    axmap  = list(axmap_buf)
    return axmap, btnmap

def _make_js_index_finders(js_index: int):
    """(find_axis_index(abs_code:int)->int, find_button_index(key_code:int)->int)"""
    fd = _open_js_fd(js_index)
    if fd is None:
        return (lambda code: -1), (lambda code: -1)
    try:
        axes, btns = _read_js_counts(fd)
        axmap, btnmap = _read_js_maps(fd)
    finally:
        os.close(fd)

    # js の “軸番号i” に対して axmap[i] == ABS_* code
    def find_axis_index(abs_code: int) -> int:
        try:
            for i, c in enumerate(axmap):
                if int(c) == int(abs_code):
                    return i
        except Exception:
            pass
        return -1

    # js の “ボタン番号i” に対して btnmap[i] == KEY/BTN_* code
    def find_button_index(key_code: int) -> int:
        try:
            # 実ボタン数 btns より長いことがあるので先頭 btns を優先
            for i, c in enumerate(btnmap[:max(0, btns)]):
                if int(c) == int(key_code):
                    return i
            # 見つからなければ全体も走査
            for i, c in enumerate(btnmap):
                if int(c) == int(key_code):
                    return i
        except Exception:
            pass
        return -1

    return find_axis_index, find_button_index
# ---------- ここまで ----------

def scan_devices(filters):
    devs = []
    for path in evdev.list_devices():
        try:
            d = evdev.InputDevice(path)
            name = d.name or "Unknown"
            if filters:
                low = name.lower()
                if not any(tok in low for tok in filters):
                    continue
            devs.append((d, name, path))
        except Exception:
            continue
    return devs

def collect_rows(filters):
    rows_axes, rows_keys = [], []
    for d, name, path in scan_devices(filters):
        # ここは “常に int コード（安定）” の verbose=False を使う
        try:
            caps = d.capabilities(verbose=False)
        except Exception:
            continue

        tag = tag_from_name(name)
        js_index = get_js_index_for_event(path)
        find_axis_index, find_button_index = _make_js_index_finders(js_index)

        # EV_ABS → 行 + js軸番号
        for code in caps.get(ecodes.EV_ABS, []):
            abs_code = _code_of(code)
            code_name = resolve_abs_name(abs_code)
            js_ax = find_axis_index(abs_code)
            rows_axes.append([js_index, name, tag, "ABS", code_name, abs_code, code_name, js_ax])

        # EV_KEY（BTN_* も KEY_* も）→ 行 + jsボタン番号
        for code in caps.get(ecodes.EV_KEY, []):
            key_code = _code_of(code)
            key_name = resolve_key_name(key_code)
            js_btn = find_button_index(key_code)
            rows_keys.append([js_index, name, tag, "KEY", key_name, key_code, key_name, js_btn])

    return rows_axes, rows_keys

def choose_default(candidates):
    """候補から既定1行を選択。優先: wheel > shift > src。候補が空なら None。"""
    if not candidates:
        return None
    order = {"wheel":0, "shift":1, "src":2}
    candidates = sorted(candidates, key=lambda r: order.get(r[2], 9))  # r[2]=src_tag
    return candidates[0], [r for r in candidates[1:]]

def emit_grouped(f, kind, virtual_order, rows):
    write_header(f, kind)
    indexed = list(enumerate(rows))
    used_idx = set()

    # 仮想名 -> 候補(インデックス付き)（src_code_name 一致ベース）
    by_vname = {v: [] for v in virtual_order}
    for idx, r in indexed:
        v = r[6]  # default_virtual（= src_code_name）
        if v in by_vname:
            by_vname[v].append((idx, r))

    for vname in virtual_order:
        cand_pairs = by_vname.get(vname, [])
        cand_rows = [r for _, r in cand_pairs]
        picked = choose_default(cand_rows) if cand_rows else None

        if picked:
            default, rest = picked
            f.write("\t".join(map(str, default)) + "\n")
            for idx, r in indexed:
                if r is default and idx not in used_idx:
                    used_idx.add(idx)
                    break
            for _, r in cand_pairs:
                if r is default:
                    continue
                f.write("#? " + "\t".join(map(str, r)) + "\n")
        else:
            # 名前一致がなければ未使用プールから自動充当（wheel>shift>src）
            pool = [r for idx, r in indexed if idx not in used_idx]
            order = {"wheel":0, "shift":1, "src":2}
            pool.sort(key=lambda r: order.get(r[2], 9))
            auto = pool[0] if pool else None
            if auto:
                f.write("\t".join(map(str, auto)) + "\n")
                for idx, r in indexed:
                    if r is auto and idx not in used_idx:
                        used_idx.add(idx); break
                f.write(f"#? (auto-assigned to {vname}; edit if needed)\n")
            else:
                f.write(f"# (no default source for {vname})\n")

        f.write("\n")  # ブロック区切り

    leftovers = [r for idx, r in indexed if idx not in used_idx]
    if leftovers:
        f.write("# --- EXTRA CANDIDATES (not auto-assigned) ---\n")
        for r in leftovers:
            f.write("#? " + "\t".join(map(str, r)) + "\n")
        f.write("\n")

def main():
    ap = argparse.ArgumentParser(description="Export mapping TSVs with default-filled blocks and JS index hints")
    ap.add_argument("--scan-names", nargs="*", default=[], help="only include device names containing any of these tokens (case-insensitive)")
    ap.add_argument("--axes", default="mapping_axes.tsv")
    ap.add_argument("--buttons", default="mapping_buttons.tsv")
    args = ap.parse_args()
    filters = [s.lower() for s in args.scan_names]

    rows_axes, rows_keys = collect_rows(filters)

    with open(args.axes, "w", encoding="utf-8", newline="") as f:
        emit_grouped(f, "axes", VIRTUAL_AXES_ORDER, rows_axes)
    with open(args.buttons, "w", encoding="utf-8", newline="") as f:
        emit_grouped(f, "buttons", VIRTUAL_BUTTONS_ORDER, rows_keys)

    print(f"[mapping] exported (default-filled + js index): {args.axes}, {args.buttons}")

if __name__ == "__main__":
    main()
