"""収集元ごとのアダプタ。

各モジュールは ``collect() -> list[DeckResult]`` を実装する。
新しい情報源を足したいときはここにファイルを1つ追加して
``src/pokeca/cli.py`` の SOURCES に登録すれば済むようにしてある。
"""
