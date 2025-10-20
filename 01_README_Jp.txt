~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
🏎️ UnderSteer — Unified Wheel & Shifter Integration Tool
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

UnderSteer は、複数の物理デバイス（ステアリングホイールとシフター）を自動検出し、
1つの仮想コントローラとして統合するためのLinux向けユーティリティです。

主な特徴:

「wheel」「shift」を名称から自動スキャンし、各デバイスをペアリング

両デバイスの軸・ボタン入力を統合して仮想コントローラへ送信

ゲームからのフォースフィードバック(FFB)を物理ホイールへ転送

python-evdev + uinput によるユーザ空間実装

このツールは、Thrustmaster・VelocityOne・Logitech などの汎用HIDデバイスに対応し、
Forza Horizon / Assetto Corsa / rFactor などのレースシミュレータで
自然な操作体験を提供します。



◆◆◆ このツールは何？

"Thrustmaster Thrustmaster Racing Wheel FFB"と
"VelocityOne Multi-Shift"の２つのUSBコントローラを組み合わせて
一つのハンドルコントローラとして認識させたい、という狙いから作成しました。

また、フォースフィードバックにも対応しました。



◆◆◆ このツールの動作イメージ

１．wheel と shift の文字列で USB 機器を探し出し、一つのデバイスとして統合します。

[i] wheel  : /dev/input/event26 | --:-- | name='Thrustmaster Thrustmaster Racing Wheel FFB' phys='usb-0000:02:00.0-1.3/input0' uniq=''
[i] shifter: /dev/input/event27 | --:-- | name='VelocityOne Multi-Shift VelocityOne Multi-Shift' phys='usb-0000:02:00.0-1.2.1/input0' uniq='TBRS004-20241224'


２．"UnderSteer FFB Wheel-Shifter" という名前のコントローラが作成されます。
    ソースコード中で Logitech G29 を偽装し、多くのゲームに認識されやすくしました。
    ハードコーディングしているため、必要に応じてソースの書き換えで変更可能です。

            name="UnderSteer FFB Wheel-Shifter",
            vendor=0x046d,   # ← Logitech, Inc.
            product=0xc24f,  # ← G29 Driving Force Racing Wheel (PS3)
            version=0x0100,  # 


３．ゲーム中では、「Logitech G29 Racing Wheel」のように認識されます。
    外付けＨシフターを認識し、１つのデバイスとして統合されます。
    さらに、フォースフィードバックも効きます。



⚙️ Steam 起動オプション について【参考】

・Forza Horizon 5 - Steam 起動オプション

SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x046d/0xc24f SDL_HIDAPI_IGNORE_DEVICES_EXCEPT=0x046d/0xc24f %command%

Forza Horizon 5 では、一つのデバイスしか読み取れなかったため、
上記の起動オプションで強制的に認識させることができました。
「Logitech G29 Racing Wheel」以外を無視するように指示する
Steam 起動コマンドです。


・Forza Horizon 6

https://youtu.be/lwlw0ChiSR0

Forza Horizon 6 出るらしい。楽しみ。


⚙️ gears.dat について【参考】

BTN_0
BTN_1
BTN_2
BTN_3
BTN_4
BTN_5
BTN_6
BTN_7

手持ちの８速シフターの、１速から８速のボタン名です。
これに起動オプションを正しく設定すると、
ニュートラルにギアが入っている間に、
ニュートラルボタンを押し続ける作りに出来ます。(BTN_DEADを使用)

Test Drive Unlimited Soler Crown で使ったように思います。



⚙️ 現時点でのヘルプ について【参考】

$ sudo python3 understeer.py --help
usage: understeer.py [-h] [--list] [--scan-names WHEEL_KW SHIFTER_KW]
                     [--wheel WHEEL] [--shifter SHIFTER]
                     [--ff-pass-through-easy] [--ff-pass-through] [--no-grab]
                     [--gear-map GEAR_MAP] [--keymap KEYMAP]
                     [--keymap-source {wheel,shift,both}] [--echo-buttons]
                     [--echo-buttons-tsv] [--vid VID] [--pid PID]
                     [--vname VNAME] [--ff-off] [-v]



UnderSteer — wheelshifter 統合仮想コントローラ

options:
  -h, --help            show this help message and exit
  --list                検出した入力デバイスを一覧表示して終了
  --scan-names WHEEL_KW SHIFTER_KW
                            自動選定に使う名前のキーワード（既定: wheel / shifter）
  --wheel WHEEL         wheel デバイスの event パスを明示指定（例: /dev/input/event21）
  --shifter SHIFTER     shifter デバイスの event パスを明示指定
  --ff-pass-through-easy
                        FF_GAIN / FF_AUTOCENTER を物理 wheel へパススルー
  --ff-pass-through     FFB Command を物理 wheel へ転送
  --no-grab             物理デバイスを grab しない（衝突注意）
  --gear-map GEAR_MAP   ギア定義ファイルのパス（ボタン名一覧）を指定すると標準ギア出力を合成
                            （G1..G8→BTN_0..BTN_7、N→BTN_DEAD）
  --keymap KEYMAP       ボタン→キーストロークのTSVファイル
  --keymap-source {wheel,shift,both}
                            キーボード送出の対象元（wheel/shift/both）
  --echo-buttons        押したボタン名をログ出力（TSV作成の補助）
  --echo-buttons-tsv    押下時に『BTN_xxx\tKEY_???』のテンプレ行も出力
  --vid VID             仮想デバイスの Vendor ID (例: 0x046d = Logitech)
  --pid PID             仮想デバイスの Product ID 
                        (例: 0xc24f = G29 Driving Force Racing Wheel PS3)
  --vname VNAME         仮想デバイス名 (任意の文字列)
  --ff-off              (temporary) Disable EV_FF on the virtual device 
                        to guarantee game startup
  -v, --verbose         ログ詳細化（-v, -vv）




【変更履歴】

2025/10/16  Ver 1.31 : Debug

2025/10/16  Ver 1.3 : Expand FFB

OK★　: FF_GAIN
OK★　: FF_AUTOCENTER

OK★　: FF_CONSTANT
OK★　: FF_SPRING
OK★　: FF_DAMPER
OK★　: FF_RUMBLE

: FF_PERIODIC
: FF_FRICTION
: FF_INERTIA
: FF_RAMP
: FF_SQUARE       (periodic subtype)
: FF_TRIANGLE     (periodic subtype)
: FF_SINE         (periodic subtype)
: FF_SAW_UP       (periodic subtype)
: FF_SAW_DOWN     (periodic subtype)

2025/10/09  Ver 1.1 : Correct FFB

2025/10/08  Ver 1.0 : Initial Commit


【作った人】

田中　秀宗 / Hidemune TANAKA









## ⚙️ Dirt Rally 対策（FFB効かないやつ向け）

### ✅ 方式1: SDLをevdev強制モードにする

以下の環境変数を追加で指定して Steam / Dirt Rally を起動：

```
 SDL_GAMECONTROLLER_IGNORE_DEVICES=0 SDL_HIDAPI_JOYSTICK=0 
```

これで HIDAPI を無効化し、`/dev/input/eventX` 経由の EV_FF を使わせます。
 （多くのゲームではこれで有効になります）
