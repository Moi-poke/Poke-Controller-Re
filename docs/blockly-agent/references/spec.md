# コンパクト仕様 v1

Poke-Controller の Blockly プログラムを、AI エージェントが機械的に組み立てるための入力形式である。`tools/blockly_agent.py` が次の順に処理する。

1. この仕様を Blockly の workspace JSON（`.blockly.json` の中身）へ組み立てる。
2. 本物の Blockly（node の vm）で Python を生成し、保存前検査を通す。
3. `build --write` で `SerialController/Commands/PythonCommands/<stem>.py` と `<stem>.blockly.json` の対を保存する。

保存済み作品の正本は `.blockly.json`、`.py` は派生物である。このコンパクト仕様は新規作成用の入力であり、保存済み作品と並ぶ正本ではない。新規作成は仕様 → `check` → `build --write`、既存作品は `check --workspace` → `patch --dry-run` → `patch --write` で扱う（[手順](../README.md)、[CLI 契約](cli.md)）。GUI 編集済み作品を古い仕様から作り直さず、workspace の ID・配置・コメント・変数を保持して部分編集する。

## トップレベル

```json
{
  "stem": "MyFirstBlockly",
  "name": "はじめてのブロック",
  "tags": ["blockly", "sample"],
  "do": [
    {"press": "A", "duration": 0.1, "wait": 0.5},
    {"wait": 1},
    {"repeat": 3, "do": [{"press": "B"}]},
    {"print": "完了"}
  ],
  "subs": [
    {"name": "aisatsu", "do": [{"print": "hello"}], "return": "ok"}
  ]
}
```

| キー | 必須 | 内容 |
| --- | --- | --- |
| `stem` | 任意（`build --write` では必須） | 保存名。`[A-Za-z_][A-Za-z0-9_]*`。拡張子は書かない。仕様に無ければ `--stem` で渡す。 |
| `name` | 必須 | 一覧に出る表示名（日本語可、空不可）。生成コードの `NAME` になる。 |
| `tags` | 任意 | 文字列または文字列の配列。既定 `["blockly"]`。文字列は `,` と `、` で分割し、重複は落とす。 |
| `do` | 任意 | プログラム本体の文の配列。空なら `pass`。 |
| `subs` | 任意 | サブルーチン定義の配列。 |

未知のトップレベルキーはエラーとなる。`variables` は自動生成されるため記述しない（書くと未知キー扱いになる）。

## 文の並び

`do`・`subs[].do`・`if.do` などの文入力には配列を指定する。配列は上から順に Blockly の `next` チェーンへ変換される。文入力は単一の dict でもよい（`{"repeat": 3, "do": {"press": "A"}}`）。

文は次のいずれかの形式で書く。

- **短縮形**: `{"press": "A"}` など（後述）。
- **汎用形**: `{"type": "...", "fields": {...}, "inputs": {...}, "extraState": {...}}`。

値ブロックを文として置くことはできない。`pokecon_vision_contains` のような値ブロックは `if` / `while` の条件や値入力へ置く。また、`break` / `continue`（`controls_flow_statements`）は後続の接続口を持たないため、必ずその並びの最後に置く。後ろに文があるとエラーになる。

## 値の書き方

値入力（`TIMES`・`TEXT`・`VALUE`・`DELTA`・`ARG0` など）には次のいずれかを書く。

| 書き方 | 生成されるブロック | 備考 |
| --- | --- | --- |
| 文字列 `"hello"` | `text`（`TEXT`） | String 入力、または check の無い入力。 |
| 数値 `3` / `0.5` | `math_number`（`NUM`） | Number 入力、または check の無い入力。 |
| 真偽値 `true` / `false` | `logic_boolean`（`BOOL`） | Boolean 入力、または check の無い入力。 |
| `{"var": "count"}` | `variables_get`（`VAR`） | 変数参照。型は自由。 |
| `{"elapsed": true}` | `pokecon_elapsed` | 開始からの経過秒（Number）。`true` のみ。 |
| `{"call_value": "sub", "args": [...]}` | `pokecon_sub_call_value` | 戻り値つきサブルーチンの呼出。値としてのみ。 |
| 汎用ブロック dict | `{"type": ...}` | 出力を持つブロックだけ。 |

入力の `check` が Boolean / Number / String のとき、リテラルの種類が合っていなければエラーになる。check が無い入力では、JSON の型から上記を選ぶ。`null` は値ではないため、不要な入力は省略する。

`{"call_value": ...}` の `args` は最大 3 個である。文として呼ぶときは `{"call": "sub", "args": [...]}` を使う。

## 変数

- `variables_set` / `variables_get` / `math_change` / `controls_for` の `VAR` は変数名の文字列で書く。builder が `{"id": 名前}` へ変換し、トップレベルの `variables` に `{"name": 名前, "id": 名前}` を重複なく足す。
- 変数名は Python の識別子である必要がある（予約語・`self` は不可）。日本語の識別子も使える。
- 設定ダイアログ（`pokecon_dialog_*`）の `VAR` は Python の変数名を表す文字列のまま保持し、`variables` には足さない。こちらは英字・数字・`_` のみ使用できる（`self`・`do`・`NAME`・`True`・`False`・`None` は不可）。

例:

```json
{"set": "count", "to": 0}
{"change": "count", "by": 1}
{"print": {"var": "count"}}
```

## サブルーチン

`subs` の各要素:

```json
{
  "name": "add",
  "args": ["a", "b"],
  "do": [{"comment": "足し算"}],
  "return": {
    "type": "math_arithmetic",
    "fields": {"OP": "ADD"},
    "inputs": {"A": {"var": "a"}, "B": {"var": "b"}}
  }
}
```

- **`name`**: 必須の識別子であり、重複は不可とする。`do`・`__init__`・`self`・`NAME`・`TAGS`・`__...__` は使用できない。
- **`args`**: 配列またはカンマ区切り文字列（`"a,b"`）で指定する（最大 3 個）。各引数は識別子とし、重複および `self` は不可とする。
- **`do`**: 文の配列を指定する。
- **`return`**: 任意の項目である。値を書くとその値を返すメソッドになる（戻り値なしのときは `-> None`）。
- **定義の配置と変換先**: 各定義は生成コードの `class BlocklyCmd` のメソッドになる。トップレベルに置く必要があり、`do` の中には置けない。
- **呼出方法**: 文として呼ぶ場合は `call`、値として呼ぶ場合は `call_value` を使う。引数は最大 3 個である。
- **検査でエラーになる条件**: 定義名の重複、未定義呼出、循環呼出（直接の再帰を含む）、引数の数・名前の不一致は `check` でエラーになる。

```json
{"call": "aisatsu", "args": [1]}
{"print": {"call_value": "add", "args": [1, 2]}}
```

## 短縮形

短縮形は 1 つの dict に 1 つだけ書く（`press` の `wait` のように、短縮形名と同名のパラメータを持つものがあるため、builder は残りのキーから主キーを判定する）。

| 短縮形 | 値の意味 | 追加項目 | 生成ブロック |
| --- | --- | --- | --- |
| `press` | ボタン・十字キー・スティック方向 | `duration`, `wait` | `pokecon_press` |
| `hold` | 押し続ける対象 | `wait` | `pokecon_hold` |
| `release` | 離す対象 | なし | `pokecon_hold_end` |
| `stick` | `L` / `R` / `L+R` | `angle`, `duration`, `wait` | `pokecon_stick` |
| `stick_off` | `L` / `R` / `L+R` | なし | `pokecon_stick_release` |
| `wait` | 待ち秒（数値） | なし | `pokecon_wait` |
| `press_rep` | 連打する対象 | `count`, `duration`, `interval`, `wait` | `pokecon_press_rep` |
| `repeat` | 回数（数値または Number の値） | `do` | `controls_repeat_ext` |
| `while` | 続ける条件（Boolean の値） | `do` | `controls_whileUntil`（`WHILE`） |
| `if` | 条件（Boolean の値） | `do`, `else` | `controls_if` |
| `break` | `true` | なし | `controls_flow_statements`（`BREAK`） |
| `continue` | `true` | なし | `controls_flow_statements`（`CONTINUE`） |
| `print` | 表示する内容 | `kind` または `result` | `pokecon_print` |
| `comment` | コメント文字列 | なし | `pokecon_comment` |
| `finish` | `true` | なし | `pokecon_finish` |
| `set` | 変数名 | `to`（値） | `variables_set` |
| `change` | 変数名 | `by`（値） | `math_change` |
| `call` | サブルーチン名 | `args`（配列） | `pokecon_sub_call` |
| `call_value` | サブルーチン名 | `args`（配列） | `pokecon_sub_call_value` |
| `screenshot` | `true` | なし | `pokecon_screenshot` |
| `discord` | 送る内容 | `kind`（`text` / `image`） | `pokecon_discord` |
| `dialog_choice` | 受け変数名、または設定オブジェクト | `var`, `title`, `label`, `options`, `default` | `pokecon_dialog_choice` |
| `dialog_number` | 受け変数名、または設定オブジェクト | `var`, `title`, `label`, `min`, `max`, `default` | `pokecon_dialog_number` |
| `dialog_check` | 受け変数名、または設定オブジェクト | `var`, `title`, `label`, `default` | `pokecon_dialog_check` |

### 基本操作

```json
{"press": "A", "duration": 0.1, "wait": 0.5}
{"press": "Hat.TOP"}
{"press": "Direction.UP"}
{"hold": "A", "wait": 0.5}
{"release": "A"}
{"stick": "L", "angle": 90, "duration": 0.2, "wait": 0.1}
{"stick_off": "L+R"}
{"wait": 1}
{"press_rep": "B", "count": 3, "duration": 0.1, "interval": 0.1, "wait": 0.1}
```

- `press` のボタンには素の名前（`A`、`B`、`ZL`、`Hat.TOP`、`Direction.UP` など）を指定する。`Button.A` も受け付けるが、内部では素の名前へ寄せる。
- `hold` / `release` / `press_rep` / 画像認識の `TARGET` は `Button.A` が正規形である。`A` と書いても `Button.A` へ寄せるが、`Hat.*` / `Direction.*` はそのまま扱う。
- `stick` の `angle` の既定値は 90 であり、強さは常に 100%（`PAD` は `角度,100`）となる。`L+R` は両スティックを表す。

### 流れ

条件には値の書き方（真偽値・比較ブロック・画像認識ブロックなど）を使う。

```json
{"repeat": 3, "do": [{"press": "A"}]}
{"repeat": {"var": "n"}, "do": [{"press": "A"}]}
```

```json
{
  "while": {
    "type": "logic_compare",
    "fields": {"OP": "LT"},
    "inputs": {"A": {"var": "count"}, "B": 10}
  },
  "do": [
    {"change": "count", "by": 1},
    {"if": {"type": "logic_compare", "fields": {"OP": "EQ"}, "inputs": {"A": {"var": "count"}, "B": 3}},
     "do": [{"print": "3"}],
     "else": [{"print": "..."}]},
    {"if": {"type": "logic_compare", "fields": {"OP": "GT"}, "inputs": {"A": {"var": "count"}, "B": 8}},
     "do": [{"break": true}]}
  ]
}
```

```json
{"if": {"var": "flag"}, "do": [{"print": "on"}], "else": [{"print": "off"}]}
{"break": true}
{"continue": true}
```

### 出力・終了

```json
{"print": "hello"}
{"print": "結果", "result": true}
{"comment": "作業メモ"}
{"finish": true}
{"screenshot": true}
{"discord": "hello", "kind": "text"}
{"discord": "画像を送る", "kind": "image"}
```

- `print` の `kind` は `print`（表示）または `print2`（結果）を指定する。`result: true` は `print2`、`result: false` は `print` になり、`kind` と `result` は同時に使わない。
- `finish` / `screenshot` / `break` / `continue` の値は `true` のみ指定できる。
- `discord` の `kind` を省略すると `text` になる。`image` を指定した場合はカメラ（ImageProc）になる。
- `screenshot` は `self.camera.saveCapture()` を呼び出す。

### 変数

```json
{"set": "count", "to": 0}
{"change": "count", "by": 1}
{"set": "name", "to": "Pikachu"}
{"set": "ok", "to": true}
```

`set` の `to` と `change` の `by` には値の書き方（リテラル・`{"var": ...}`・ブロック）を指定する。`change` の `by` には Number の値を指定する。

### サブルーチン呼出

```json
{"call": "aisatsu", "args": ["x"]}
{"print": {"call_value": "add", "args": [1, 2]}}
```

### 設定ダイアログ

```json
{"dialog_choice": {"var": "mode", "title": "モード", "label": "選ぶ", "options": ["A", "B"], "default": "A"}}
{"dialog_number": {"var": "count", "title": "回数", "label": "回数", "min": 1, "max": 3, "default": 2}}
{"dialog_check": {"var": "confirm", "title": "確認", "label": "実行する", "default": true}}
```

- 値は受け変数名の文字列だけでもよい（`{"dialog_choice": "mode"}`）。その場合、`title` などはブロックの既定値が使われる。
- `options` には文字列の配列、または `"A,B"` のようなカンマ区切り文字列を指定し、`default` には選択肢の文字列を指定する。
- `dialog_number` の `min` / `max` / `default` は数値で指定し、`min <= max` を満たす必要がある（保存前検査で検証される）。
- `dialog_check` の `default` は真偽値で指定する。
- 受け変数名には英字・数字・`_` を使う。予約語・`self`・`do`・`NAME` などは使用できない。
- ダイアログの `VAR` は Blockly の変数ではない。`{"var":"mode"}` で参照すると、通常の変数参照として `variables` に足される（生成コードは同じ名前の Python 変数を読む）。

## 汎用形

```json
{
  "type": "pokecon_vision_contains",
  "fields": {"TEMPLATE": "controller_check/btn_a.png", "THRESHOLD": 0.8, "USE_GRAY": true},
  "inputs": {},
  "extraState": {}
}
```

- **`type`**: 必須である。カタログ（`references/blocks.md` または `catalog`）にあるブロックだけ使える。
- **`fields`**: カタログの `fields` にある名前だけを指定する。値を省略した欄にはブロックの既定値が使われる。
- **`inputs`**: カタログの `inputs` にある名前だけを指定する。文入力は配列または単一 dict、値入力は値の書き方で記述する。
- **`extraState`**: Blockly へそのまま渡す（代表例は後述）。
- **配置制限**: `pokecon_program` と `pokecon_sub_def` はトップレベル専用であり、`do` の中の汎用形としては置けない。
- **表示専用フィールド**: `PREVIEW` / `CAPOPEN` は、仕様に書いても無視される。

### フィールドの値

| 種類 | 書き方 |
| --- | --- |
| `number` | 数値（`0.5` など）。真偽値は不可。 |
| `checkbox` | `true` / `false`（`"TRUE"` / `"FALSE"` も可）。 |
| `variable` | 変数名の文字列（設定ダイアログの `VAR` を除く）。 |
| `controller` | ボタン名。`Button.A` / `Hat.TOP` / `Direction.UP`、または素の `A`。 |
| `template` | `Template/` からの相対名。空は不可。 |
| `crop` | 空、または `x1,y1,x2,y2`（実画素、`x2>x1`、`y2>y1`）。 |
| `stickpad` | `角度,強さ`（例 `90,100`）。通常は `stick` 短縮形を使う。 |
| `text` | 文字列。選択肢のある欄は候補の値だけ。 |

### `extraState` の例

#### `controls_if` の else / else if

`else` だけ:

```json
{
  "type": "controls_if",
  "extraState": {"hasElse": true},
  "inputs": {
    "IF0": true,
    "DO0": [{"print": "成立"}],
    "ELSE": [{"print": "不成立"}]
  }
}
```

`else if` を足すときは `elseIfCount`（**I は大文字**）と `IF1` / `DO1` を使う:

```json
{
  "type": "controls_if",
  "extraState": {"elseIfCount": 1, "hasElse": true},
  "inputs": {
    "IF0": true,
    "DO0": [{"print": "A"}],
    "IF1": {"type": "logic_boolean", "fields": {"BOOL": "FALSE"}},
    "DO1": [{"print": "B"}],
    "ELSE": [{"print": "C"}]
  }
}
```

通常は短縮形 `if` の `else` を使う方が安全である。

#### `text_join` の `itemCount`

```json
{
  "type": "text_join",
  "extraState": {"itemCount": 2},
  "inputs": {"ADD0": "a", "ADD1": "b"}
}
```

`itemCount` は結合する個数を表す。現在のカタログには `ADD0` / `ADD1` だけがあるため、`inputs` に書けるのは 2 個までとなる。3 個以上を結合する場合は `text_join` を入れ子にするか、`text_join` を 2 回に分ける。`text_join` の `itemCount` を 3 以上にしても、`ADD2` 以降は builder が未知入力として落とす。

#### `controls_for`

```json
{
  "type": "controls_for",
  "fields": {"VAR": "i"},
  "inputs": {
    "FROM": 0,
    "TO": 2,
    "BY": 1,
    "DO": [{"print": {"var": "i"}}]
  }
}
```

#### `controls_repeat` / `controls_repeat_ext`

- `controls_repeat`: `fields: {"TIMES": 3}` と `inputs.DO` を使い、回数は**フィールド**で指定する。
- `controls_repeat_ext`: `inputs: {"TIMES": 3, "DO": [...]}` を使い、回数は**入力**で指定する。短縮形 `repeat` はこちらを使う。

## 標準ブロック

使える標準ブロックは次の 20 個である（カタログにあるものだけ使用でき、これ以外の標準ブロックはエラーになる）。

`controls_repeat` / `controls_repeat_ext` / `controls_whileUntil` / `controls_for` / `controls_if` / `controls_flow_statements` / `logic_compare` / `logic_operation` / `logic_negate` / `logic_boolean` / `math_number` / `math_arithmetic` / `math_modulo` / `math_change` / `math_random_int` / `math_random_float` / `text` / `text_join` / `variables_get` / `variables_set`

主な欄:

- `logic_compare`: `OP` = `EQ` / `NEQ` / `LT` / `LTE` / `GT` / `GTE`、`A` / `B`（値）。
- `logic_operation`: `OP` = `AND` / `OR`、`A` / `B`（Boolean）。
- `logic_negate`: `BOOL`（Boolean）。
- `logic_boolean`: `BOOL` = `TRUE` / `FALSE`。
- `math_arithmetic`: `OP` = `ADD` / `MINUS` / `MULTIPLY` / `DIVIDE` / `POWER`、`A` / `B`（Number）。
- `math_modulo`: `DIVIDEND` / `DIVISOR`（Number）。
- `math_change`: `VAR`（変数）+ `DELTA`（Number）。
- `math_random_int`: `FROM` / `TO`（Number）。
- `math_random_float`: 入力はない。
- `text`: `TEXT`（文字列）。
- `variables_get` / `variables_set`: `VAR`（変数）。`variables_set` には `VALUE` 入力もある。
- `controls_whileUntil`: `MODE` = `WHILE` / `UNTIL`、`BOOL`、`DO`。
- `controls_flow_statements`: `FLOW` = `BREAK` / `CONTINUE`（後続の接続口はない）。

## 検査の流れ

`check` / `build --write` は次の順で検査する。1 つでも落ちれば保存しない。

1. builder の構造検査を行う。
   - 仕様のキー・型および `stem` / `name` / `tags` を検査する。
   - ブロック種・フィールド・入力の存在（候補付き）を検査する。
   - フィールドの型・選択肢・ボタン名・変数名を検査する。
   - `TEMPLATE` が空でないこと、および `Template/<名前>` が実在することを確認する。
   - `CROP` の書式と `x2>x1` / `y2>y1`、および `THRESHOLD` の 0〜1 範囲を検査する。
   - 文の並び（`break` / `continue` が最後にあるか）を検査する。
   - サブルーチンの定義・呼出・引数・循環を検査する。
2. `blockly_validate.validate_workspace_json` で workspace JSON の形式を検査する。
3. `blockly_validate.validate_dialog_vars` で設定ダイアログの受け変数と数値範囲を検査する。
4. 本物の Blockly で workspace を読み込み、`workspaceToCode` が通るかを確認する。
5. `blockly_validate.validate_generated_code` で import 公開面、`NAME` / `do`、サブルーチンの循環・引数を検査する。
6. `blockly_templates.validate_template_refs` で生成コード内のテンプレート参照の書式（絶対パス・`..`・`:`）を検査する。
7. `build --write` は既存の `save_blockly` が同じ検査を再実行し、`.py` の sha256 を `.blockly.json` の `pokecon.pySha256` に記録して原子的に保存する。

## 典型的なエラー

| エラー | 直し方 |
| --- | --- |
| `ブロック \`pokecon_pres\` は使えません（似た名前: ...）` | カタログの正しいブロック名へ直す。 |
| `press の項目 \`duraton\` は使えません（似た名前: duration）` | 短縮形のパラメータ名を直す。 |
| `ブロック \`pokecon_print\` に input \`TXET\` はありません（似た名前: TEXT）` | `inputs` の名前を直す。 |
| `この入力は Boolean 型です（pokecon_elapsed は使えません）` | 条件には Boolean を返すブロック（比較・画像認識・真偽値）を使う。 |
| `TEMPLATE が空です` | `Template/` の画像名を指定する。 |
| `TEMPLATE の画像が見つかりません: 'x.png'` | 実在する画像名へ直す。無ければユーザーに撮ってもらう。 |
| `CROP は空か \`x1,y1,x2,y2\` で書いてください` | 書式を直す。 |
| `CROP は x2>x1 かつ y2>y1 にしてください` | 右上の座標を左下より大きくする。 |
| `THRESHOLD は 0〜1 で書いてください` | 0〜1 の値にする。 |
| `未定義のサブルーチンです: self.x()（do から）` | `subs` に定義を足すか、呼出名を直す。 |
| `引数の数が違います: self.x()（do から: 1個、定義は2個）` | 呼出の `args` を定義に合わせる。 |
| `サブルーチンの循環呼び出しがあります: self.a() -> self.b() -> self.a()` | 呼出の向きを直す。 |
| `do: \`controls_flow_statements\` の後ろに文を置けません` | `break` / `continue` を最後に移す。 |
| `保存名（stem）を書いてください` | 仕様の `stem` か `--stem` を付ける。 |
| `node が見つかりません（Blockly のヘッドレス生成に必要）` | node を PATH に通す。 |

## 制約・落とし穴

- 標準ブロックはカタログにある 20 個だけである。`controls_forEach` や `lists_*` などは使えない。
- `pokecon_program` / `pokecon_sub_def` はトップレベル専用であり、プログラムとサブルーチンは `do` / `subs` から生成される。
- `break` / `continue` は文の並びの最後だけに置ける。
- `wait` の値には数値のみ指定できる（ブロックに値入力が無いため、値ブロックは使えない）。
- `set` / `change` の値（変数名）は文字列で指定し、`to` / `by` に値を指定する。
- `print` の `kind` と `result` は同時に使えない。
- `stick` の強さは常に 100% である。`PAD` を直接いじるより `stick` 短縮形を使う。
- 設定ダイアログの `VAR` は英字・数字・`_` のみ使用できる。通常の変数は Python の識別子（日本語可）とする。
- 表示専用の `PREVIEW` / `CAPOPEN` は仕様に書いても無視される。
- `text_join` の `ADD2` 以降は現行カタログに無いため、3 要素以上を結合する場合は入れ子にする。
- `elseIfCount` の `I` は大文字である。`elseifCount` は Blockly が読まない。
- `stem` は `[A-Za-z_][A-Za-z0-9_]*` に従う。`build --write` では必須である。
- `.py` を直接編集しない。手編集を検出した通常保存は拒否され、明示許可を得た場合だけ `--overwrite-external` を使う（[手順](../README.md)）。
- 既存作品を上書きするときはユーザーの意図を確認し、保存済み workspace から `patch` する。ディスクの版検査はブラウザの未保存編集を検出しない。

## 実行時引数（workspace / patch）

`POKECON_INPUTS`（Start 前の引数）と、その値を使うブロックは、現行のコンパクト仕様には専用の短縮形が**ない**。新規に組むときは workspace JSON を直接書くか、`patch` の `create_block` / `set_field` / `connect` で作る。

| ブロック | 主な fields / inputs |
| --- | --- |
| `pokecon_input_choice` | `KEY`, `TITLE`, `SET`, `MULTI`, `DEFAULT`, `LABELS`（`値=表示名;…`） |
| `pokecon_input_int` | `KEY`, `TITLE`, `MIN`, `MAX`, `DEFAULT`, `ALLOW_UNLIMITED`, `DEFAULT_UNLIMITED` |
| `pokecon_input_get` | `KEY`。値ブロック |
| `pokecon_option_template` | `SET` + `VALUE`。値から実在する画像パスを返す |
| `pokecon_for_each` | `VAR` + `LIST`、`DO` |
| `pokecon_repeat_limit` | `VAR` + `LIMIT`、`DO`（上限が None なら回り続ける） |
| `pokecon_list_has` / `pokecon_list_remove` / `pokecon_list_empty` | `LIST`（+ `VALUE`） |

宣言ブロックはトップレベルに置く（プログラムの外でもよい）。画像認識ブロックの `TEMPLATE_EXPR` につないだ式は固定の `TEMPLATE` 欄より優先される。

## 関連

- [手順](../README.md) — 作成・保存済み作品の部分編集・ライブ編集。
- [cli.md](cli.md) — CLI オプション、操作 JSON、版と結果の読み方。
- [blocks.md](blocks.md) — 生成されたブロック一覧。手で編集しない。
- [examples/](../examples/) — 動く作例。
