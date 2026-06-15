# Youtube_maker

```text
Youtube_maker/                        ← プロジェクトルート
│
├── scripts/                          ← スクリプト一式
│   ├── manager.py
│   ├── theme_generator.py
│   ├── genkou_generater.py
│   ├── genkou_checker.py
│   ├── image_generator.py
│   ├── image_checker.py
│   ├── movie_generater.py
│   ├── youtube_uploader.py
│   ├── path_helper.py
│   ├── log_writer.py
│   └── auth.py
│
├── channels/
│   └── {チャンネル名}/               ← チャンネルごとに1フォルダ
│       ├── config/
│       │   ├── manager_config.ini
│       │   ├── theme_gen_config.txt
│       │   ├── genkou_checker_config.ini
│       │   ├── image_prompt.ini
│       │   └── uploader_config.ini
│       ├── prompts/
│       │   ├── theme_system.txt
│       │   ├── common.txt
│       │   ├── paradox.txt
│       │   └── genkou_prompt.txt
│       ├── data/
│       │   ├── themes.csv            ← 自動生成・更新
│       │   └── genkou.csv            ← 自動生成・更新
│       ├── output/
│       │   └── result/               ← 生成された.mp4
│       └── logs/                     ← 実行ログ
│
└── common/
    └── config/
        ├── client_secret.json       ← 自分で取得
        └── token.pickle             ← 認証後に自動生成
