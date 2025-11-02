#!/usr/bin/env bash
# find_wheel_vidpid.sh
set -euo pipefail

pairs=()

shopt -s nullglob
for d in /sys/bus/usb/devices/*; do
  prod_file="$d/product"
  ven_file="$d/idVendor"
  pid_file="$d/idProduct"

  [[ -f "$prod_file" && -f "$ven_file" && -f "$pid_file" ]] || continue

  # 製品名に "wheel" が含まれるか判定（大文字小文字無視）
  if grep -qi 'wheel' "$prod_file"; then
    ven=$(<"$ven_file")
    pid=$(<"$pid_file")
    # 0xを付けて整形
    pairs+=( "0x${ven}/0x${pid}" )
  fi
done

# 重複除去
if ((${#pairs[@]}==0)); then
  echo "No USB device whose product name contains 'wheel' was found." >&2
  exit 1
fi

# uniq（順序維持）
unique=()
for p in "${pairs[@]}"; do
  seen=0
  for q in "${unique[@]}"; do
    [[ "$p" == "$q" ]] && { seen=1; break; }
  done
  (( seen==0 )) && unique+=( "$p" )
done

joined=$(IFS=, ; echo "${unique[*]}")

echo ""
cat <<EOS
[First-time only]
Add the following settings:

$ sudo nano /etc/udev/rules.d/99-understeer.rules
---
# Add VID/PID to UnderSteer uinput event node
KERNEL=="event*", ATTRS{name}=="*UnderSteer*", \
  ENV{ID_VENDOR_ID}="${ven}", ENV{ID_MODEL_ID}="${pid}", \
  ENV{ID_VENDOR_ENC}="UnderSteer", ENV{ID_MODEL_ENC}="Virtual Wheel", \
  ENV{ID_INPUT_JOYSTICK}="1"
---
After editing, run the following:

$ sudo udevadm control --reload && sudo udevadm trigger


[Settings required for each Steam game]
Set the following in Steam's launch options.

SDL_GAMECONTROLLER_IGNORE_DEVICES=0 SDL_HIDAPI_JOYSTICK=0 SDL_GAMECONTROLLER_IGNORE_DEVICES=${joined} SDL_HIDAPI_IGNORE_DEVICES=${joined} %command%


[for Xbox 360 Controller]

Example)

SDL_GAMECONTROLLER_IGNORE_DEVICES=03004ad34f04000096b6000011010000 SDL_GAMECONTROLLERCONFIG="0300ca40dead0000beef000000010000,Xbox 360 Controller understeer,leftx:a0,lefty:a1,lt:a2,rightx:a3,righty:a4,rt:a5,,dpup:h0.1,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,a:b5,b:b4,x:b3,y:b2,back:b21,start:b22,guide:b12,lb:b0,rb:b1,leftstick:b6,rightstick:b7,platform:Linux" %command%

...
EOS
