# Pi03でのnasne HDD保全とDLM生成

対象は、先行検証機と同じ純正v2.60の起動DLMを持つ **CECH-ZNR2J**。HDDを接続してから手動でスクリプトを実行し、本体ラベルのIDと対象HDDのシリアル番号を入力する。

## Pi03に用意するもの

- Raspberry Pi OS / DebianのPython 3.11以降、`git`、`gpg`、`ssh-keygen`、`lsblk`、`blkid`、`sfdisk`。NASのマウント先（メニューの既定値は `/mnt/k3snas01`） に、専用の `nasne-backups` ディレクトリを作れる権限。
- このリポジトリの `nasne-prepare.py` と、SonyNasne-debianのクリーンなチェックアウト。固定コミットは `95addd8c76d2c4b82efb0dfa9d4a568ae88fdfd3`。root専用の `/opt/nasne-prep` 以下に置く。
- 先行検証で作成し、このスクリプトの `TEMPLATE_SHA` に一致する `debian-install.dlm` を `/opt/nasne-prep/pilot-template.dlm` として転送する。テンプレートは検証機のSSHホスト秘密鍵を含むため、root所有でモード600にする。生成時にそのホスト鍵を除き、新しいホスト鍵を組み込む。
- 管理者用のRSA**公開鍵**を `/opt/nasne-prep/admin.pub` に置く。NASバックアップ暗号化用のパスフレーズをroot専用の `/opt/nasne-prep/nas-passphrase` に置き、別の安全な場所にも保管する。

対象HDDをPi03に接続し、ターミナルでメニューを開く。処理を選ぶとNAS共有元の入力を求める。`NASNE_NAS_SOURCE` で共有元、`NASNE_NAS_MOUNT` でマウント先を指定できる。

```sh
sudo python3 /opt/nasne-prep/nasne-prepare.py
```

| 番号 | メニュー | 操作 |
|---|---|---|
| 1 | ディスク一覧 | HDDのSERIALと `/dev/disk/by-id/` のリンクを表示。末尾に `-part1` が付くリンクはパーティションを指す。 |
| 2 | HDDのバックアップ | `sys1`、純正DLM、パーティション表、HDDシリアルを保存し、NASの暗号化コピーを読み戻す。SPI採取用と復旧用のDLMも生成する。空のHDD、nasneの起動領域・起動ファイルを確認できないHDD、交換用として登録されたHDDは自動で対象外にする。 |
| 3 | HDDの復元 | 元のHDDの`sys1`を復元するか、交換用HDDを初期化して純正用に再構築する。 |
| 4 | Debian化 | HDDバックアップと対象個体を照合し、Debian導入用 `00550066.dlm` を生成する。 |
| 0 | 終了 | メニューを閉じる。 |

**SPIの保全**。メニュー2で生成した `spi-tools/spi-backup-only.dlm` を対象HDDに配置し、nasne本体で起動する。採取後にHDDをPi03へ戻し、メニュー2を再度実行する。スクリプトがSPIダンプの二重読み出しの結果、ハッシュ、カーネル領域のCRCを検査し、NAS上の暗号化コピーを復号照合する。Debian導入用DLMはメニュー4で作成する。SPI保全を検証してから、DLMの配置と初回起動へ進む。

**元のHDDの復元**。メニュー3で「元のHDDの`sys1`を復元」を選ぶ。メニュー2で作ったバックアップをPi03またはNASから確認し、元のシリアル番号とパーティション表が一致したときに、確認文字列を入力して`sys1`を書き戻す。処理後にハッシュを読み戻す。保全対象は起動領域で、録画番組の復元には録画領域のバックアップが必要。

**交換用HDDの復元**。メニュー3で「交換用HDDを初期化・再構築」を選び、元のnasneの本体IDと交換先HDDのリンク・SERIALを入力する。Pi03に公式v2.60ファームウェアパッケージを `/opt/nasne-prep/KRST3101_0260_SECURE.dlm` として置く。交換先は論理セクタ512バイトのUSB接続ディスクで、容量は4 GiBから2 TiBまで。確認文字列を入力すると、シリアル番号を交換用として作業フォルダに記録し、交換先の全データを消去する。以降、そのシリアルのHDDは同じ作業フォルダのバックアップ対象から外れる。同じ本体の正常なバックアップがあれば、保存した純正起動マネージャーから本体固有IDを読み、上流ツールで純正起動ファイルと交換先HDDの登録情報を作成する。Pi03のバックアップが使えない場合は、NASの暗号化コピーを復号して照合する。

**バックアップがない場合**。スクリプトがID採取用DLMを配置して終了する。交換用HDDを対象nasneに入れて起動し、2〜3分後に電源を切る。同じHDDをPi03に戻し、同じ本体IDとシリアル番号でメニュー3の交換用HDDを再実行する。スクリプトが採取したIDを検証して再開する。IDの採取時に録画領域にファイルが作られた場合は一覧を表示し、その区画だけを再初期化する確認を求める。

本体ID、交換先のシリアル番号、パーティション表、作業段階を記録する。途中で別のHDDを指定した場合は停止する。全体初期化の完了が記録される前に中断した場合は、対象HDDの状態を確認する。作成後は同じnasne本体で純正起動を確認する。本体のSPIカーネル・NV設定の復旧やDebian環境・録画データの復元は、それぞれの保全データを用いる。

出力先は /var/backups/nasne-prepare/<本体ID>/。先行検証機で手動採取した旧バックアップを復元メニューで使う場合は、形式の移行が必要。

一覧だけを表示するには `--list-disks` を使う。引数で実行する場合は `--action` に `backup`、`debian`、`restore-original`、`restore-replacement` のいずれかを指定する。その他の引数は `--help` で確認できる。

- [SonyNasne-debianのSPI採取・復旧手順](https://github.com/mar1mo-41414/SonyNasne-debian/blob/main/docs/16_spi_backup_dlm.md)
- [SonyNasne-debianのDebian導入手順](https://github.com/mar1mo-41414/SonyNasne-debian/blob/main/docs/13_full_setup_from_official_hdd.md)
