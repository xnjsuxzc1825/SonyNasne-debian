#!/usr/bin/env python3
"""Back up, restore or prepare one registered nasne HDD on Linux."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile

STOCK_SHA = "39a3a436bace9c3fa4a252a9272cf5715afb6e0b3af695c76d73050aa22014c8"
OFFICIAL_SHA = "3f6ed5bc451de006c3309bc1c37e0ece11edd686d641853601cc1296e8084987"
TEMPLATE_SHA = "41858ac81b1d7649ba71447886c69e7e16efec75da33c48e7a1a63a2361e7303"
UPSTREAM_REV = "95addd8c76d2c4b82efb0dfa9d4a568ae88fdfd3"
SPI_SIZES = {"spi_mtd0.bin": 13631488, "spi_mtd1_nv.bin": 3080192}


def run(*cmd):
    subprocess.run(cmd, check=True)


def output(*cmd):
    return subprocess.check_output(cmd, text=True).strip()


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def regular_copy(source, dest):
    require(stat.S_ISREG(source.lstat().st_mode), f"Not a regular file: {source}")
    shutil.copyfile(source, dest)


@contextlib.contextmanager
def phase(root, name):
    final = root / name
    with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=root) as temp:
        stage = Path(temp) / name
        stage.mkdir()
        yield stage
        run("sync", "-f", stage)
        stage.rename(final)
        run("sync", "-f", root)


@contextlib.contextmanager
def mounted(part, fstype="ext3", options="ro,noload"):
    with tempfile.TemporaryDirectory(prefix="nasne-sys1-", dir="/run") as temp:
        mount = Path(temp)
        run("mount", "-t", fstype, "-o", options, part, mount)
        try:
            yield mount
        finally:
            run("umount", mount)


def identify_disk(args):
    disk = args.disk.resolve(strict=True)
    require(stat.S_ISBLK(disk.stat().st_mode), "Expected a block device")
    require(output("lsblk", "-dnro", "TYPE,TRAN", disk) == "disk usb", "Expected a USB disk")
    require(bool(args.serial) and output("lsblk", "-dnro", "SERIAL", disk) == args.serial,
            "Disk serial mismatch")
    require(not output("lsblk", "-nro", "MOUNTPOINTS", disk), "HDD is already mounted")
    return disk


def partition(args, disk, number):
    part = args.disk.with_name(f"{args.disk.name}-part{number}").resolve(strict=True)
    require(stat.S_ISBLK(part.stat().st_mode), "Expected a partition block device")
    require(output("lsblk", "-dnro", "PKNAME", part) == disk.name, "Partition parent mismatch")
    return part


def identify(args):
    disk = identify_disk(args)
    try:
        part = partition(args, disk, 1)
    except FileNotFoundError as exc:
        raise ValueError("起動領域がありません。未初期化のHDDはバックアップ対象外です。") from exc
    try:
        metadata = output("blkid", "-p", "-o", "export", part).splitlines()
    except subprocess.CalledProcessError as exc:
        raise ValueError("先頭パーティションの形式を確認できません。") from exc
    require("LABEL=sys1" in metadata and "TYPE=ext3" in metadata
            and output("blockdev", "--getsize64", part) == "268435456",
            "nasneの起動領域（sys1、ext3、256 MiB）が見つかりません。")
    return disk, part


def write_manifest(directory):
    lines = [f"{digest(file)}  {file.name}" for file in sorted(directory.iterdir())
             if file.is_file()]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def gpg_command(args, root):
    require(args.passphrase.stat().st_uid == 0 and
            args.passphrase.stat().st_mode & 0o077 == 0 and args.passphrase.stat().st_size > 0,
            "Passphrase file must be nonempty and readable only by root")
    return ["gpg", "--homedir", str(root / "gnupg"), "--batch", "--no-tty",
            "--pinentry-mode", "loopback", "--passphrase-file", str(args.passphrase)]


def publish(args, root, phase_name):
    # Check that the destination is the expected NAS share, even on a retry.
    require(output("findmnt", "-nro", "SOURCE", "--mountpoint", args.nas_mount)
            == args.nas_source, "NAS mount/source mismatch")
    source = root / phase_name
    subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=source, check=True)
    dest_dir = args.nas_mount / "nasne-backups" / args.device
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{phase_name}.tar.gpg"
    gpg = gpg_command(args, root)
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=root) as temp:
        plain = Path(temp) / "snapshot.tar"
        with tarfile.open(plain, "w") as archive:
            for file in sorted(source.iterdir()):
                archive.add(file, arcname=file.name, recursive=False)
        expected = digest(plain)

        def verify(path):
            with subprocess.Popen([*gpg, "--decrypt", str(path)], stdout=subprocess.PIPE) as proc:
                actual = hashlib.file_digest(proc.stdout, "sha256").hexdigest()
                require(proc.wait() == 0 and actual == expected, f"NAS verification failed: {path}")

        if target.exists():
            verify(target)
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=dest_dir)
            os.close(fd)
            upload = Path(temp_name)
            try:
                run(*gpg, "--yes", "--cipher-algo", "AES256", "--output", upload,
                    "--symmetric", plain)
                verify(upload)
                run("sync", upload)
                upload.rename(target)
            finally:
                upload.unlink(missing_ok=True)
            run("sync", "-f", dest_dir)
            verify(target)
    print(f"NAS backup verified: {target}", flush=True)


def build(args, official, result, mode, *extra):
    builder = args.upstream / "scripts/stage1/make_stage1_dlm.py"
    run(sys.executable, "-IB", builder, "build", "--official", official, "--out", result,
        "--mode", mode, "--telnet", "off", *extra)
    require(result.stat().st_size > 64, "Generated DLM is empty")


def prepare_debian_tree(args, tree, keys):
    keys.mkdir()
    require(digest(args.template) == TEMPLATE_SHA, "Unexpected pilot template SHA-256")
    public = args.admin_key.read_bytes().strip()
    require(re.fullmatch(rb"ssh-rsa [A-Za-z0-9+/=]+(?: [^\r\n]*)?", public),
            "Provide one RSA public key")
    run("ssh-keygen", "-lf", args.admin_key)
    for algo, bits in (("rsa", "3072"), ("ecdsa", "256")):
        run("ssh-keygen", "-q", "-m", "PEM", "-t", algo, "-b", bits, "-N", "",
            "-f", keys / f"ssh_host_{algo}_key")
    (keys / "authorized_keys").write_bytes(public + b"\n")
    with args.template.open("rb") as raw:
        raw.seek(64)
        with tarfile.open(fileobj=raw, mode="r|gz") as original, tarfile.open(tree, "w") as archive:
            for item in original:
                if not item.name.startswith("debian/"):
                    continue
                name = item.name.removeprefix("debian/")
                if (name.startswith("etc/ssh/ssh_host_") or name in
                        {"root/.ssh/authorized_keys", "etc/nasne-install-mode"}):
                    continue
                if item.islnk():
                    require(item.linkname.startswith("debian/"), "Unexpected hard link")
                    item.linkname = item.linkname.removeprefix("debian/")
                item.name = name
                item.pax_headers.pop("path", None)
                item.pax_headers.pop("linkpath", None)
                body = original.extractfile(item) if item.isfile() else None
                archive.addfile(item, body)
            for key in keys.iterdir():
                name = ("root/.ssh/" if key.name == "authorized_keys" else "etc/ssh/") + key.name
                entry = archive.gettarinfo(key, arcname=name)
                entry.uid = entry.gid = 0
                entry.uname = entry.gname = "root"
                entry.mode = 0o644 if key.suffix == ".pub" else 0o600
                with key.open("rb") as file:
                    archive.addfile(entry, file)


def disk_record(args):
    serial_key = hashlib.sha256(args.serial.encode()).hexdigest()
    return args.work / ".disks" / serial_key


def backup(args, root):
    require(not disk_record(args).with_suffix(".replacement").exists(),
            "交換用HDDではバックアップを実行できません。元のHDDを接続してください。")
    disk, part = identify(args)
    stock = root / "hdd"
    if stock.exists():
        require((stock / "disk-serial.txt").read_text().strip() == args.serial,
                "登録済みの元のHDDとシリアル番号が異なります。")
    artifacts = root / "spi-tools"
    if not artifacts.exists() and (root / "dlm" / "spi-backup-only.dlm").exists():
        artifacts = root / "dlm"
    spi = root / "spi"
    official_name = "00550066.dlm"
    if artifacts.exists():
        subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=artifacts, check=True)
    with mounted(part) as mount:
        current = mount / official_name
        manager = mount / "00110022.dlm"
        require(all(file.is_file() and not file.is_symlink() for file in (current, manager)),
                "nasneの起動ファイルがそろっていません。バックアップ対象外です。")
        current_sha = digest(current)
        if not stock.exists():
            require(current_sha == STOCK_SHA, "Stock DLM differs from validated CECH-ZNR2J pilot")
            require(not (mount / "spi_backup").exists(), "Unexpected existing SPI backup")
            rebuild(args, "verify", mount)
            chip_id(args, mount)
            with phase(root, "hdd") as stage:
                for name in ("00110022.dlm", official_name):
                    regular_copy(mount / name, stage / name)
                require(digest(stage / official_name) == STOCK_SHA, "Stock DLM changed during copy")
                (stage / "disk-serial.txt").write_text(args.serial + "\n")
                (stage / "partitions.sfdisk").write_text(output("sfdisk", "--dump", disk) + "\n")
                run("dd", f"if={part}", f"of={stage / 'sys1.img'}", "bs=4M", "status=none")
                require((stage / "sys1.img").stat().st_size == 268435456, "Incomplete sys1 image")
                require(digest(stage / "sys1.img") == digest(part), "sys1 image readback mismatch")
                write_manifest(stage)
        require(digest(stock / "00110022.dlm") == digest(mount / "00110022.dlm"),
                "Stock manager mismatch")
        require(current_sha == STOCK_SHA or (artifacts.exists() and
                current_sha == digest(artifacts / "spi-backup-only.dlm")), "Unexpected DLM")
        if current_sha != STOCK_SHA and not spi.exists():
            with phase(root, "spi") as stage:
                for name in (*SPI_SIZES, "backup.log", "MD5SUMS", "SHA1SUMS", "README.txt"):
                    regular_copy(mount / "spi_backup" / name, stage / name)
                for name, size in SPI_SIZES.items():
                    require((stage / name).stat().st_size == size, f"Unexpected size: {name}")
                log = (stage / "backup.log").read_text()
                require(all(f"mtd{n} 結果 rc=0" in log for n in (0, 1)), "SPI double-read failed")
                for algo, checksum in (("md5", "MD5SUMS"), ("sha1", "SHA1SUMS")):
                    lines = (stage / checksum).read_text().splitlines()
                    expected = [f"{hashlib.new(algo, (stage / name).read_bytes()).hexdigest()}  {name}"
                                for name in SPI_SIZES]
                    require(lines == expected, f"SPI checksum mismatch: {checksum}")
                info = output(sys.executable, "-IB", args.upstream / "scripts/spi_ab_info.py",
                              stage / "spi_mtd0.bin")
                require(all(re.search(rf"^{name}\s+検証=OK\b", info, re.M)
                            for name in ("KNL", "BKNL")), "SPI kernel CRC verification failed")
                (stage / "spi-slots.txt").write_text(info + "\n")
                regular_copy(artifacts / "spi-restore.dlm", stage / "spi-restore.dlm")
                write_manifest(stage)

    publish(args, root, "hdd")
    if not artifacts.exists():
        with phase(root, "spi-tools") as stage:
            require(digest(args.template) == TEMPLATE_SHA, "Unexpected pilot template SHA-256")
            with args.template.open("rb") as raw:
                raw.seek(64)
                with tarfile.open(fileobj=raw, mode="r|gz") as archive:
                    for member in archive:
                        if member.name == "debian/usr/local/sbin/mtdtool" and member.isfile():
                            with (stage / "mtdtool").open("wb") as tool:
                                shutil.copyfileobj(archive.extractfile(member), tool)
                            break
            official = stock / official_name
            build(args, official, stage / "spi-backup-only.dlm", "backup", "--dest", "sys1")
            build(args, official, stage / "spi-restore.dlm", "restore", "--mtdtool", stage / "mtdtool")
            (stage / "mtdtool").unlink()
            write_manifest(stage)
    print(f"SPI DLMs: {artifacts}", flush=True)
    if spi.exists():
        publish(args, root, "spi")
    else:
        print("SPI backup pending: start the nasne with its backup-only DLM, then reconnect the HDD.",
              flush=True)


def rebuild(args, *command):
    run(sys.executable, "-IB", args.upstream / "scripts/nasne_hdd_rebuild.py", *command)


def chip_id(args, stock):
    result = output(sys.executable, "-IB", args.upstream / "scripts/nasne_hdd_rebuild.py",
                    "read-id", stock / "00110022.dlm")
    match = re.search(r"^個体ID: ([0-9a-f]{16})$", result, re.M)
    require(match and match[1] != "0" * 16, "No valid chip ID in the backed-up manager")
    return match[1]


def verify_hdd_backup(args, root):
    stock = root / "hdd"
    require((stock / "SHA256SUMS").is_file(), "Run HDD backup for this nasne first")
    publish(args, root, "hdd")
    require(digest(stock / "00550066.dlm") == STOCK_SHA, "Unexpected backed-up stock DLM")
    return stock


def debian(args, root):
    stock = verify_hdd_backup(args, root)
    require((stock / "disk-serial.txt").read_text().strip() == args.serial, "Backup disk serial mismatch")
    _, part = identify(args)
    with mounted(part) as mount:
        require(digest(mount / "00110022.dlm") == digest(stock / "00110022.dlm"),
                "Stock manager mismatch")
        current = digest(mount / "00550066.dlm")
        capture = root / "spi-tools" / "spi-backup-only.dlm"
        require(current == STOCK_SHA or (capture.is_file() and current == digest(capture)),
                "Unexpected DLM on the target HDD")
    if (root / "spi").is_dir():
        publish(args, root, "spi")
    else:
        print("SPI backup pending. Verify it before installing the Debian DLM.")
    if not (root / "dlm").exists():
        with phase(root, "dlm") as stage, tempfile.TemporaryDirectory(prefix=".build-", dir=root) as temp:
            scratch = Path(temp)
            tree = scratch / "debian-tree.tar"
            prepare_debian_tree(args, tree, scratch / "keys")
            build(args, stock / "00550066.dlm", stage / "00550066.dlm", "embed",
                  "--debian-tree-tar", tree, "--p3-mode", "image", "--image-size", "8G")
            for key in (scratch / "keys").glob("ssh_host_*_key.pub"):
                shutil.copyfile(key, stage / key.name)
            write_manifest(stage)
    subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=root / "dlm", check=True)
    print(f"Debian DLM: {root / 'dlm' / '00550066.dlm'}")


def normalized_layout(layout):
    match = re.search(r"^device:\s*(/dev/\S+)$", layout, re.M)
    require(match, "No disk device in partition table")
    return layout.strip().replace(match[1], "/dev/DISK")


def confirm_restore(args, disk, target):
    require(sys.stdin.isatty(), "Restore requires an interactive terminal")
    print(output("lsblk", "-dno", "NAME,SIZE,MODEL,SERIAL", disk))
    phrase = f"RESTORE {args.device} {args.serial}"
    require(input(f"{target}を書き換えます。実行するには {phrase} を入力 > ").strip() == phrase,
            "Restore cancelled")
    require(identify_disk(args) == disk, "Target disk changed")


@contextlib.contextmanager
def restore_backup(args, root):
    try:
        stock = root / "hdd"
        require((stock / "SHA256SUMS").is_file(), "No local HDD backup")
        subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=stock, check=True)
        require(digest(stock / "00550066.dlm") == STOCK_SHA, "Unexpected backed-up stock DLM")
    except (OSError, ValueError, subprocess.CalledProcessError):
        require(output("findmnt", "-nro", "SOURCE", "--mountpoint", args.nas_mount)
                == args.nas_source, "NAS mount/source mismatch")
        source = args.nas_mount / "nasne-backups" / args.device / "hdd.tar.gpg"
        require(source.is_file(), "No usable HDD backup on Pi03 or NAS")
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=root) as temp:
            stock = Path(temp)
            plain = stock / "hdd.tar"
            run(*gpg_command(args, root), "--output", plain, "--decrypt", source)
            limits = {"sys1.img": 268435456, "00550066.dlm": 40 * 1024**2,
                      "00110022.dlm": 4096, "partitions.sfdisk": 4096,
                      "disk-serial.txt": 4096, "SHA256SUMS": 4096}
            with tarfile.open(plain, "r:") as archive:
                members = archive.getmembers()
                require(len(members) == len(limits) and {m.name for m in members} == limits.keys(),
                        "Unexpected files in NAS HDD backup")
                for member in members:
                    require(member.isfile() and 0 < member.size <= limits[member.name],
                            f"Invalid backup member: {member.name}")
                    with archive.extractfile(member) as data, (stock / member.name).open("xb") as file:
                        shutil.copyfileobj(data, file)
            plain.unlink()
            manifest = (stock / "SHA256SUMS").read_text().splitlines()
            expected = {f"{digest(stock / name)}  {name}" for name in limits if name != "SHA256SUMS"}
            require(len(manifest) == len(expected) and set(manifest) == expected,
                    "NAS HDD backup checksum mismatch")
            require(digest(stock / "00550066.dlm") == STOCK_SHA, "Unexpected backed-up stock DLM")
            print("NAS上のHDDバックアップを復号し、ハッシュを照合しました。")
            yield stock
    else:
        yield stock


def restore_original(args, stock):
    disk = identify_disk(args)
    old_serial = (stock / "disk-serial.txt").read_text().strip()
    require(args.serial == old_serial, "Select the HDD that was backed up")
    expected = normalized_layout((stock / "partitions.sfdisk").read_text())
    require(normalized_layout(output("sfdisk", "--dump", disk)) == expected,
            "Original partition table changed")
    part = partition(args, disk, 1)
    image = stock / "sys1.img"
    require(image.stat().st_size == 268435456 and
            output("blockdev", "--getsize64", part) == "268435456", "Unexpected sys1 size")
    confirm_restore(args, disk, "元のHDDのsys1")
    require(partition(args, disk, 1) == part and
            normalized_layout(output("sfdisk", "--dump", disk)) == expected,
            "Target partition changed")
    run("dd", f"if={image}", f"of={part}", "bs=4M", "conv=fsync", "status=progress")
    require(digest(part) == digest(image), "Restored sys1 readback mismatch")
    print("Original sys1 restored and verified.")
    return

def save_replacement_state(path, state, step, **updates):
    state.update(updates, step=step)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".replacement-",
                                     delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(state, file)
            file.flush()
            os.fsync(file.fileno())
            temp.replace(path)
            run("sync", "-f", path.parent)
        finally:
            temp.unlink(missing_ok=True)


def replacement_backup_id(args, root, disk):
    stock_serial = expected_layout = None
    try:
        with restore_backup(args, root) as stock:
            stock_serial = (stock / "disk-serial.txt").read_text().strip()
            expected_layout = normalized_layout((stock / "partitions.sfdisk").read_text())
            cid = chip_id(args, stock)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"バックアップから本体固有IDを取得できません。本体で採取します。 ({exc})")
        cid = None
    if stock_serial == args.serial and expected_layout is not None:
        try:
            layout_intact = disk_layout(disk) == expected_layout
        except subprocess.CalledProcessError:
            layout_intact = False
        require(not layout_intact, "元のHDDは「元のHDDのsys1を復元」を使ってください。")
    return cid


def disk_layout(disk):
    return normalized_layout(output("sfdisk", "--dump", disk))


def restore_replacement(args, root):
    disk = identify_disk(args)
    require(args.official_package and digest(args.official_package) == OFFICIAL_SHA,
            "Provide the verified stock 2.60 firmware package with --official-package")
    require(output("blockdev", "--getss", disk) == "512", "Replacement requires 512-byte logical sectors")
    size = int(output("blockdev", "--getsize64", disk))
    require(4 * 1024**3 <= size <= 2 * 1024**4, "Replacement requires 4 GiB to 2 TiB")
    for tool in ("wipefs", "sfdisk", "partprobe", "mkfs.ext3", "mkfs.xfs", "udevadm"):
        require(shutil.which(tool), f"Missing tool: {tool}")

    path = root / "replacement.json"
    state = json.loads(path.read_text()) if path.is_file() else None
    if state is not None:
        require(state.get("device") == args.device, "Replacement record belongs to another nasne")
        if state.get("serial") != args.serial:
            require(state.get("step") == "complete", "前回の作業とHDDのシリアル番号が異なります。")
            state = None
        else:
            require(state.get("size") == size, "Replacement disk size changed")
    if state is None:
        marker = disk_record(args).with_suffix(".replacement")
        require(not marker.exists(), "このHDDは別の交換作業に登録済みです。")
        cid = replacement_backup_id(args, root, disk)
        confirm_restore(args, disk, "交換用HDD全体（全データ消去）")
        state = {"device": args.device, "serial": args.serial, "size": size, "chip_id": cid}
        save_replacement_state(path, state, "formatting")
        marker.touch(exist_ok=True)
        run("sync", "-f", marker.parent)
        rebuild(args, "mkdisk", disk, "--serial", args.serial)
        run("udevadm", "settle")
        require(identify_disk(args) == disk, "Target disk changed")
        save_replacement_state(path, state, "formatted", layout=disk_layout(disk))

    step = state["step"]
    require(step != "formatting", "初期化の完了を確認できません。対象HDDの状態を個別に確認してください。")
    require(step in {"formatted", "awaiting-id", "identified", "sys1-ready", "complete"},
            "Unknown replacement step")
    require(disk_layout(disk) == state.get("layout"), "Replacement partition table changed")
    if step == "complete":
        print("この交換用HDDは作成済みです。同じnasne本体で純正起動を確認してください。")
        return

    part1 = partition(args, disk, 1)
    if step == "formatted" and not state["chip_id"]:
        with mounted(part1, options="rw") as mount:
            rebuild(args, "phase1", args.official_package, mount)
            run("sync", "-f", mount)
        save_replacement_state(path, state, "awaiting-id")
        print("HDDを対象nasneに入れて起動し、2〜3分後に電源を切ってください。"
              "同じHDDをPi03へ戻し、同じ本体IDで交換用HDDの復元を再実行してください。")
        return

    if step == "awaiting-id":
        with mounted(part1) as mount:
            manager = mount / "00110022.dlm"
            require(manager.is_file() and not manager.is_symlink(),
                    "本体固有IDはまだ採取されていません。同じnasneで起動してから再実行してください。")
            cid = chip_id(args, mount)
        save_replacement_state(path, state, "identified", chip_id=cid)

    cid = state["chip_id"]
    require(isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{16}", cid) and cid != "0" * 16,
            "Invalid saved chip ID")
    if state["step"] != "sys1-ready":
        with mounted(part1, options="rw") as mount:
            rebuild(args, "final", args.official_package, mount, "--chipid", cid)
            rebuild(args, "verify", mount)
            run("sync", "-f", mount)
        save_replacement_state(path, state, "sys1-ready")

    part3 = partition(args, disk, 3)
    with mounted(part3, fstype="xfs", options="ro,norecovery") as mount:
        entries = sorted(item.name for item in mount.iterdir() if item.name != "lost+found")
    if entries:
        print("p3の内容:", ", ".join(map(repr, entries)))
        confirm_restore(args, disk, "交換用HDDのp3（この領域の全データ消去）")
        require(partition(args, disk, 3) == part3 and disk_layout(disk) == state["layout"],
                "Target partition changed")
        rebuild(args, "mkdisk", disk, "--serial", args.serial, "--p3-only")
    with mounted(part3, fstype="xfs", options="rw") as mount:
        rebuild(args, "p3init", mount, "--chipid", cid, "--device", disk)
        run("sync", "-f", mount)
    save_replacement_state(path, state, "complete")
    print("交換用HDDを作成しました。同じnasne本体で純正起動を確認してください。")


def list_disks():
    run("lsblk", "-d", "-o", "NAME,TRAN,SIZE,MODEL,SERIAL,WWN")
    run("ls", "-l", "/dev/disk/by-id/")


def menu_arguments():
    while True:
        print("\n1. ディスク一覧\n2. HDDのバックアップ\n3. HDDの復元\n4. Debian化\n0. 終了")
        choice = input("番号を選択してください > ").strip()
        if choice == "0":
            return None
        if choice == "1":
            list_disks()
        elif choice in ("2", "3", "4"):
            action = {"2": "backup", "3": "restore", "4": "debian"}[choice]
            if action == "restore":
                kind = input("1. 元のHDDのsys1を復元 / 2. 交換用HDDを初期化・再構築 / 0. 戻る > ").strip()
                if kind not in ("1", "2"):
                    continue
                action = "restore-original" if kind == "1" else "restore-replacement"
            if os.geteuid() != 0:
                print("この操作を実行するには sudo で起動してください。")
                continue
            device = input("本体ラベルのID（nasne-xxxxxx） > ").strip()
            disk = input("ディスク全体の /dev/disk/by-id/ リンク > ").strip()
            serial = input("対象HDDの SERIAL > ").strip()
            if not all((device, disk, serial)):
                print("本体ID、ディスクのリンク、SERIALをすべて入力してください。")
                continue
            nas_mount = os.environ.get("NASNE_NAS_MOUNT", "/mnt/k3snas01")
            nas_source = os.environ.get("NASNE_NAS_SOURCE") or input("NAS共有元（例 //nas.example/NAS01） > ").strip()
            if not nas_source:
                print("NAS共有元を入力してください。")
                continue
            return [
                "--action", action,
                "--official-package", "/opt/nasne-prep/KRST3101_0260_SECURE.dlm",
                "--model", "CECH-ZNR2J",
                "--device", device,
                "--disk", disk,
                "--serial", serial,
                "--work", "/var/backups/nasne-prepare",
                "--nas-mount", nas_mount,
                "--nas-source", nas_source,
                "--passphrase", "/opt/nasne-prep/nas-passphrase",
                "--upstream", "/opt/nasne-prep/upstream",
                "--template", "/opt/nasne-prep/pilot-template.dlm",
                "--admin-key", "/opt/nasne-prep/admin.pub",
            ]
        else:
            print("1、2、3、4、0のいずれかを入力してください。")


def main():
    argv = sys.argv[1:]
    if argv == ["--list-disks"]:
        list_disks()
        return
    if not argv and sys.stdin.isatty():
        argv = menu_arguments()
        if argv is None:
            return

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Start without arguments in a terminal for the menu, or use --list-disks alone.",
    )
    parser.add_argument("--action", required=True, choices=["backup", "debian", "restore-original", "restore-replacement"])
    parser.add_argument("--official-package", type=Path)
    parser.add_argument("--model", required=True, choices=["CECH-ZNR2J"])
    parser.add_argument("--device", required=True, help="Physical unit ID from the nasne label")
    parser.add_argument("--disk", required=True, type=Path, help="Whole HDD under /dev/disk/by-id")
    parser.add_argument("--serial", required=True, help="Expected lsblk SERIAL for that HDD")
    parser.add_argument("--work", required=True, type=Path, help="Private local backup directory")
    parser.add_argument("--nas-mount", required=True, type=Path)
    parser.add_argument("--nas-source", required=True)
    parser.add_argument("--passphrase", required=True, type=Path)
    parser.add_argument("--upstream", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--admin-key", required=True, type=Path)
    args = parser.parse_args(argv)
    os.umask(0o077)
    require(os.geteuid() == 0, "Run as root on Pi03")
    require(re.fullmatch(r"nasne-[0-9a-f]{6}", args.device), "Invalid device ID")
    require(args.disk.parent == Path("/dev/disk/by-id") and not re.search(r"-part\d+$", args.disk.name),
            "Specify the whole HDD under /dev/disk/by-id")
    require(output("git", "-C", args.upstream, "rev-parse", "HEAD") == UPSTREAM_REV,
            "Unexpected upstream revision")
    require(not output("git", "-C", args.upstream, "status", "--porcelain", "--untracked-files=all"),
            "Upstream checkout has local changes")
    root = args.work / args.device
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(root.stat().st_uid == 0 and root.stat().st_mode & 0o077 == 0,
            "Device workspace must be private and owned by root")
    (root / "gnupg").mkdir(mode=0o700, exist_ok=True)
    record = disk_record(args)
    record.parent.mkdir(mode=0o700, exist_ok=True)
    require(record.parent.stat().st_uid == 0 and record.parent.stat().st_mode & 0o077 == 0,
            "Disk registry must be private and owned by root")
    with (root / "lock").open("a") as lock, record.with_suffix(".lock").open("a") as disk_lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(disk_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "backup":
            backup(args, root)
        elif args.action == "debian":
            debian(args, root)
        elif args.action == "restore-original":
            with restore_backup(args, root) as stock:
                restore_original(args, stock)
        else:
            restore_replacement(args, root)


if __name__ == "__main__":
    try:
        main()
    except EOFError:
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        sys.exit(str(exc))
