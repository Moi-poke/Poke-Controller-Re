# 音声対応（取込・検知・モニター再生）

キャプチャボードの音声を取り込み、特定の音を自動化のトリガーにしたり、ゲーム音を PC から再生（モニター）したりできるようにする。

## 前提

- ゲーム音はキャプチャボードからしか取れない。PC のマイク等では Switch の音は取れない（配線上の制約）。
- 開けないデバイスがある。WDM-KS 系は PortAudio の制約で開けない。48 kHz 専用機は本体が自レートで開き、内部で 44.1 kHz へ変換する。
- 旧スクリプト [`Commands/PythonCommands/listen_shiny.py`](../SerialController/Commands/PythonCommands/listen_shiny.py)（PyAudio 直掴み・AUX ケーブル前提）もそのまま動く。新規に作成する場合は下記の本体機能を使う。

## 画面の使い方（Audio欄）

- **Input**: 取込口を指定する。空の場合は起動時にキャプチャボードを自動選択する（カメラ名から推定）。手動で選び直すこともできる。候補は試し開きで「開ける物だけ」に絞られる（起動直後は全件が表示され、数秒後に確定する）。
- **Output**: モニター再生の出力先を指定する。表示の `[est. XXms]` は申告遅延の目安である。
- **Monitor**: チェックを入れるとゲーム音を PC から再生する（既定は OFF）。実測で約 120 ms 遅れて聞こえる（機器によって変わる）。
- **Level**: 直近の入力レベルを表示する。振れていれば音声を取り込めている。
- **Test Rec 3s**: 3 秒録音して `AudioClips/` へ保存する（疎通確認用）。
- **遅延計測**: A ボタンを 3 回押して Switch を鳴らし、押下から検知までの実遅延を測る。操作確認画面など「押すと音が出る画面」で使用する。シリアル未接続の状態では測れない。

## コマンド開発者向け

`AudioPythonCommand` を継承すると音声 API が使える（`ImageProcPythonCommand` の音声版。両方必要な場合は多重継承する）。

- `waitTone(bands, thresholds, window_s, timeout)`: 指定帯域がすべて閾値を超えるまで待つ。例は [`Commands/PythonCommands/listen_shiny2.py`](../SerialController/Commands/PythonCommands/listen_shiny2.py) を参照する（平常時を測って自動較正する書き方である）。
- `waitSound(template_wav, threshold, ...)`: 登録音（`Template/audio/<pack>/*.wav`）との一致を待つ。
- `recordClip(seconds, name)`: 直近の録音を `AudioClips/` へ保存する。
- `onSoundDetected(...)` / `watchSounds(...)`: 検知時のコールバックで駆動する。

しきい値は機種や音量で変わるため、絶対値の決め打ちではなく `listen_shiny2.py` のように平常時比で決める。利用者スクリプトの公開面（`task userapi`）の範囲内で記述し、`band_power` / `is_tone` は `Commands.CommandAudio` から読める。

## 制限・既知事項

- モニター再生は約 120 ms 遅れる。用途は確認用であり実況用ではない。
- 申告遅延（est）は機器の自己申告であり、実測とずれる場合がある。出力選びの目安とし、迷ったら遅延計測の実測と聞き比べで決める。
- 録音・検知の内部レートは 44.1 kHz mono に統一している。
- 音量が小さい入力では検知の感度が落ちる。まず Level 表示で確認する。
