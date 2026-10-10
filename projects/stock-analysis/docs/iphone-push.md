# iPhone でプッシュ通知を受け取るための手順（未実施・承認待ち）

> **この作業は、Webアプリを外部（インターネット）から見られるようにする「公開」にあたります。**
> 指示書のルールにより、利用者の承認を得てから行います。下の手順はまだ実行していません。

## 必要な条件

| 条件 | 内容 | 根拠 |
|---|---|---|
| iOS のバージョン | iOS / iPadOS **16.4 以降** | https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/ |
| ホーム画面に追加 | Safari で開き、「共有 → ホーム画面に追加」で追加したアプリからのみ通知を受け取れる | 同上 |
| 通知の許可 | ボタンを押すなど、利用者の操作をきっかけに許可を求める（アプリの「この端末で通知を受け取る」ボタンで対応済み） | 同上 |
| HTTPS | 安全な接続（https）が必要。自宅PCの `http://127.0.0.1` には iPhone から届かない | Web Push の一般的な要件（Apple 公式の原文は未確認） |
| Service Worker・VAPID鍵 | 実装済み（`frontend/public/sw.js`）。鍵は `setup.bat` が自動で作る | — |

## 推奨構成：Cloudflare Tunnel（無料）

**仕組み**
- 自宅PCから Cloudflare へ外向きの接続を張ります。ルーターのポート開放は不要です。
- 発行される `https://〜` の URL に iPhone からアクセスします。

**安全対策（二重の鍵）**
1. **アプリの認証**：`STOCK_APP_TOKEN` が必須です。トンネル経由の通信は、トークンがなければ必ず拒否します（実装・テスト済み）。
2. **Cloudflare Access（無料・50人まで）**：URL を開く前に、登録したメールアドレスへ届くワンタイムコードでの確認を求めます。第三者はアプリの画面にもたどり着けません。

### 手順（承認後に実施）

1. Cloudflare の無料アカウントを作ります。
   - 独自ドメインがない場合は、お試し用の一時URL（Quick Tunnel）も使えます。ただし URL が毎回変わり、Access も使えないため、常用には向きません。
2. ターミナルで cloudflared を入れます。
   ```
   winget install Cloudflare.cloudflared
   ```
3. Cloudflare の管理画面（Zero Trust → Networks → Tunnels）でトンネルを作り、表示されたコマンドを実行します。
   - 宛先（Service）は `http://localhost:8000` を指定します。
4. Zero Trust → Access → Applications でアプリを追加し、ポリシーを「自分のメールアドレスのみ許可」にします。
5. iPhone の Safari で `https://（発行されたURL）/#token=（setup.bat が表示したトークン）` を開きます。
6. 「共有 → ホーム画面に追加」を選び、追加したアイコンからアプリを開きます。
7. 「通知・システム」タブの「この端末で通知を受け取る」を押し、通知を許可します。
8. PCのターミナルで次を実行し、iPhone にテスト通知が届けば完了です。
   ```
   cd C:\Augma\address-mapper-pro\projects\stock-analysis\backend
   .venv\Scripts\python -m stock_analysis test-push
   ```

### 費用
- Cloudflare Tunnel・Access（50ユーザーまで）は無料です。
- 独自ドメインを使う場合は、ドメイン代として年1,000〜2,000円程度かかります（任意）。

## 承認をお願いしたいこと
- Cloudflare Tunnel でアプリを外部に公開してよいか
- Cloudflare Access（メールのワンタイムコード）を必須にしてよいか（推奨）
- 独自ドメインを使うか、まずは一時URLで試すか
