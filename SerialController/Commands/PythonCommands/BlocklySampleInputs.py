from Commands.Keys import Button
from Commands.PythonCommandBase import ImageProcPythonCommand

i = None
item = None


class BlocklyCmd(ImageProcPythonCommand):
    NAME = "入力つき収集の例"
    TAGS = ["blockly", "Sample"]
    POKECON_INPUTS = [
        {
            "key": "max_rounds",
            "kind": "int",
            "min": 1,
            "max": 100,
            "title": "上限周回",
            "default": 10,
            "allow_unlimited": True,
        },
        {
            "key": "targets",
            "kind": "choice",
            "set": "controller_check",
            "multi": True,
            "title": "集める対象",
            "default": "btn_a,btn_b",
            "labels": {
                "btn_a": "A",
                "btn_b": "B",
                "btn_lclick": "Lクリック",
                "hat_top": "十字キー上",
                "header": "ヘッダ",
            },
        },
    ]

    def __init__(self, cam, gui=None):
        super().__init__(cam, gui)

    def do(self) -> None:
        _pokecon_limit_for = self.input_value("max_rounds")
        i = 0
        while _pokecon_limit_for is None or i < _pokecon_limit_for:
            i += 1
            for item in list(self.pokecon_list(self.input_value("targets"))):
                if self.isContainTemplate(
                    self.option_template("controller_check", item),
                    threshold=0.7,
                    use_gray=False,
                ):
                    self.press(Button.A, duration=0.1, wait=0.1)
                    self.pokecon_list_remove(self.input_value("targets"), item)
                else:
                    self.wait(0.2)
        print(self.input_value("targets"))
