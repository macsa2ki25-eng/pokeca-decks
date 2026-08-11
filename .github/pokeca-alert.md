`healthcheck` が失敗しました。新しいシティリーグの結果が10日以上取れていません。

収集元サイトのHTML構造が変わり、パーサーが空振りしている可能性があります。
（大会が開催されていない長期休みなどの場合もあるので、まず日付を確認してください）

## 調べ方

```bash
python -m src.pokeca.cli inspect --source pokecabook
python -m src.pokeca.cli inspect --source official
```

`data/pokeca/_inspect/` に実物のHTML・JSONが保存されます。
その構造を見て `src/pokeca/sources/` のパーサーを直してください。

直したら、実物に合わせたテストケースを `tests/test_pokeca.py` に追加してから
このIssueを閉じてください。
