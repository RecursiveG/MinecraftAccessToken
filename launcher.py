#!/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright 2021-2022, Recursive G
# This project is opensourced under the MIT license.
#
'''Minecraft launcher

List available versions:

  ./launcher.py

Generate launch arguments:

  ./launcher.py --dotmc_folder <dir> --version <version> --gamedir <dir> --argfile <output>
'''

from functools import reduce
from absl import app
from absl import flags
from pathlib import Path
import copy
import httplib2
import json
import hashlib
import subprocess
import uuid
import mslogin
import re

FLAGS = flags.FLAGS

flags.DEFINE_string("dotmc_folder", None, ".minecraft folder")
flags.DEFINE_string("version", None, "Minecraft version")
flags.DEFINE_string("gamedir", None, "Gamedir")

flags.DEFINE_string("argfile", None, "output path of the arg file")
flags.DEFINE_string("offline", None, "Offline mode username")
flags.DEFINE_list("extra_natives", None, "Extra native binaries to load")

http = httplib2.Http()

#======= helpers =======#


def httpget(url):
    print("Downloading", url)
    (resp, content) = http.request(url)
    assert resp["status"] == "200", "Failed to GET " + url
    return content


def download_file(url: str, dst: Path, hash=None):
    if dst.exists():
        # check hash if file exists
        if hash is not None:
            actual_hash = hashlib.sha1(open(dst, "rb").read()).hexdigest()
            if actual_hash == hash:
                return
        else:
            print(f"[warn] No HASH for {str(dst)}, cannot check integrity")

    # download and check hash
    dst.parent.mkdir(0o755, True, True)
    with open(dst, "wb") as f:
        f.write(httpget(url))
    if hash is not None:
        actual_hash = hashlib.sha1(open(dst, "rb").read()).hexdigest()
        assert hash == actual_hash, f"url={url} path={str(dst)} hash={hash} actual={actual_hash}"


#======= version.json =======#


def download_version_manifest():
    url = 'https://launchermeta.mojang.com/mc/game/version_manifest.json'
    return json.loads(httpget(url))


# Recursivly load versions by inheritance. Download file if necessary.
# Return a list of loaded json structures, base version first.
def download_version_chain(ver: str, versions_map):
    ret = []
    while ver is not None:
        version_dir = Path(FLAGS.dotmc_folder) / "versions" / ver
        version_json = version_dir / (ver + ".json")
        version_dir.mkdir(mode=0o755, parents=True, exist_ok=True)

        # download json file
        if ver in versions_map:
            json_url = versions_map[ver]["url"]
            json_hash = re.search(r'v1/packages/([a-f0-9]*)/', json_url)[1]
            download_file(json_url, version_json, json_hash)
        assert version_json.is_file(), f"{ver}.json not found, is it an official version?"

        # load json file
        version_obj = json.load(open(version_json, 'r'))
        ret.append(version_obj)

        # continue to next version
        ver = version_obj.get("inheritsFrom")    # default=None

    ret = ret[::-1]
    return ret


# normalize the version json object so that they can be merged
# Returns the changed input obj.
def normalize_version_obj(ver_obj):
    if "minecraftArguments" in ver_obj:
        assert "arguments" not in ver_obj
        ver_obj["arguments"] = dict(
            game=ver_obj["minecraftArguments"].split(' '),
            jvm=r'-Djava.library.path=${natives_directory} -cp ${classpath}'.split(' '),
        )
        del ver_obj["minecraftArguments"]

    return ver_obj


# Returns the modified base.
def merge_version_objs(base, overlay):
    VERSION_JSON_INHERIT_OVERWRITE = {"id", "time", "releaseTime", "type", "mainClass"}
    VERSION_JSON_INHERIT_IGNORE = {"_comment_", "inheritsFrom", "logging"}

    for k, v in overlay.items():
        if k in VERSION_JSON_INHERIT_IGNORE:
            continue
        elif k in VERSION_JSON_INHERIT_OVERWRITE:
            base[k] = v
        elif k == 'libraries':
            base[k] += v
            pass
        elif k == "arguments":
            base[k]["game"] += v["game"]
            base[k]["jvm"] += v["jvm"]
        else:
            assert False, f"unexpected inhertance field: {k}"

    return base


#======= main jar and assets =======#


# returns the main jar version name
def download_main_jar(normalized_versions) -> str:
    for obj in reversed(normalized_versions):
        if "id" not in obj: continue
        ver = obj["id"]
        jar = Path(FLAGS.dotmc_folder) / "versions" / ver / f"{ver}.jar"
        # Why does Fabric put an empty jar there?
        if jar.is_file() and jar.stat().st_size > 0:
            return ver
        if "downloads" in obj:
            u = obj["downloads"]["client"]["url"]
            h = obj["downloads"]["client"]["sha1"]
            download_file(u, jar, h)
            return ver
    assert False, "Cannot determine main jar version."


def download_asset(merged_obj):
    obj = merged_obj
    ai = obj["assetIndex"]

    skindir = Path(FLAGS.dotmc_folder) / "assets/skins"
    skindir.mkdir(mode=0o755, parents=True, exist_ok=True)

    p = Path(FLAGS.dotmc_folder) / "assets/indexes" / (ai["id"] + ".json")
    download_file(ai["url"], p, ai["sha1"])
    ai_obj = json.load(open(p, "r"))

    for info in ai_obj["objects"].values():
        h = info["hash"]
        u = f"https://resources.download.minecraft.net/{h[:2]}/{h}"
        p = Path(FLAGS.dotmc_folder) / f"assets/objects/{h[:2]}/{h}"
        download_file(u, p, h)


#======= libraries and natives =======#


# Returns Dict[osx|linux|windows, need_to_load:bool]
def parse_libraries_rules(rules):
    allow = dict(osx=False, linux=False, windows=False)
    disallow = dict(osx=False, linux=False, windows=False)
    for r in rules:
        if r["action"] == "allow":
            if "os" in r:
                allow[r["os"]["name"]] = True
            else:
                allow = dict(osx=True, linux=True, windows=True)
        else:
            if "os" in r:
                disallow[r["os"]["name"]] = True
            else:
                disallow = dict(osx=True, linux=True, windows=True)
    ret = dict()
    for x in ["osx", "linux", "windows"]:
        ret[x] = allow[x] and not disallow[x]
    return ret


# TODO remove this
def conflicting_library_resolution(group_and_name, old_lib, incoming_lib):
    # A set of jars that forge wants to update
    # FORGE_OVERWRITES = {
    #     "org.apache.commons:commons-lang3", "net.sf.jopt-simple:jopt-simple", "org.apache.logging.log4j:log4j-api",
    #     "org.apache.logging.log4j:log4j-core", "org.apache.logging.log4j:log4j-slf4j18-impl"
    # }

    if incoming_lib == old_lib:
        return old_lib

    if "natives" in old_lib and "natives" not in incoming_lib:
        return old_lib
    else:
        return incoming_lib

    # if "natives" in incoming_lib and "natives" not in old_lib:
    #     return incoming_lib

    # elif group_and_name in FORGE_OVERWRITES:
    #     return incoming_lib
    # else:
    #     assert False, f"Duplicated library {group_and_name}\n{json.dumps(old_lib, indent=2)}\n{json.dumps(incoming_lib,indent=2)}"


def dedup_library_entries(entries):
    ret = dict()
    for l in entries:
        nc = l["name"].split(":")
        assert len(nc) == 3 or len(nc) == 4
        lib_group_and_name = nc[0] + ":" + nc[1]

        ret[lib_group_and_name] = l    # Just overwrite
    return ret


def build_library_map(merged_ver):
    obj = merged_ver
    libraries = list()
    natives = list()

    # construct library list and natives list
    for l in obj["libraries"]:
        # Skip library based on rule
        if "rules" in l and not parse_libraries_rules(l["rules"])["linux"]:
            continue

        # "group:artifactid:version[:classifier][@extension]"
        X86_NATIVE_CLASSIFIERS = {"linux-x86_64", "natives-linux"}
        NATIVE_CLASSIFIERS = {"linux-x86_64", "natives-linux", "linux-aarch_64"}
        ALL_CLASSIFIERS = {"linux-x86_64", "natives-linux", "linux-aarch_64", "api", None}
        ALL_EXTENSIONS = {"jar"}

        # strip ext
        tmp = l["name"].split("@")
        assert len(tmp) == 1 or len(tmp) == 2
        if len(tmp) == 2: assert tmp[1] in ALL_EXTENSIONS

        # classify
        nc = tmp[0].split(":")
        assert len(nc) == 3 or len(nc) == 4
        classifier = nc[3] if len(nc) == 4 else None
        assert classifier in ALL_CLASSIFIERS, f"bad classifier {classifier}"

        if classifier in NATIVE_CLASSIFIERS:
            if nc[3] not in X86_NATIVE_CLASSIFIERS: continue
            natives.append(l)

        elif "url" in l:
            # Jar-only old library url struct used by fabric
            assert "downloads" not in l
            repo = l["url"]
            del l["url"]
            group, name, ver = l["name"].split(":")
            group_path = group.replace(".", "/")
            path = f"{group_path}/{name}/{ver}/{name}-{ver}.jar"
            url = f"{repo}{group_path}/{name}/{ver}/{name}-{ver}.jar"
            l["downloads"] = {"artifact": dict(url=url, path=path, sha1=None)}
            libraries.append(l)

        else:
            # Jar and native combined entry
            lib_copy = copy.deepcopy(l)
            native_copy = copy.deepcopy(l)

            # jar part
            if "artifact" in lib_copy["downloads"]:
                lib_copy.pop("natives", None)
                lib_copy.pop("extract", None)
                lib_copy["downloads"].pop("classifiers", None)
                libraries.append(lib_copy)

            # native part
            if "natives" in native_copy:
                assert native_copy["natives"]["linux"] == "natives-linux"
                tmp = native_copy["downloads"]["classifiers"]["natives-linux"]
                native_copy["downloads"] = dict(artifact=tmp)
                native_copy.pop("extract", None)
                natives.append(native_copy)

            # extra check
            if "extract" in l:
                assert l["extract"] == dict(exclude=["META-INF/"])

    return dedup_library_entries(libraries), dedup_library_entries(natives)


def download_libraries(entry_list):
    # Downloads .minecraft/libraries/*
    libdir = Path(FLAGS.dotmc_folder) / "libraries"
    seen_path = set()
    for l in entry_list:
        p = libdir / l["downloads"]["artifact"]["path"]
        u = l["downloads"]["artifact"]["url"]
        h = l["downloads"]["artifact"]["sha1"]
        assert p not in seen_path, "Duplicate download path: " + p
        seen_path.add(p)
        download_file(u, p, h)


def extract_natives(natives_map, version):
    import shutil

    # extract native binaries to .minecraft/versions/<version>/native
    libdir = Path(FLAGS.dotmc_folder) / "libraries"
    nativedir = Path(FLAGS.dotmc_folder) / "versions" / version / "native"

    print("Extracting natives...")
    nativedir.mkdir(mode=0o755, parents=True, exist_ok=True)
    for l in natives_map.values():
        native_obj = l["downloads"]["artifact"]
        jarfile = libdir / native_obj["path"]
        actual_sha = hashlib.sha1(open(jarfile, "rb").read()).hexdigest()
        assert actual_sha == native_obj["sha1"]
        print(f"SHA1 OK: {jarfile.name}")

        subprocess.run(
            ["7z", "e", "-y", "-ir!*.so", jarfile.resolve(), f"-o{nativedir.resolve()}"],
            check=True,
            stdout=subprocess.DEVNULL)

    for n in FLAGS.extra_natives:
        dst_f = nativedir / Path(n).name
        print(f"Extra native: {n} => {str(dst_f)}")
        shutil.copyfile(n, dst_f)


# ===== commandline assembling ===== #


def parse_arguments_game_rules(rules):
    # append an argument if the corresponding feature is ON
    FEATURES_ON = "has_custom_resolution".split(" ")
    FEATURES_OFF = "is_demo_user has_quick_plays_support is_quick_play_singleplayer is_quick_play_multiplayer is_quick_play_realms".split(" ")

    assert len(rules) == 1
    assert rules[0]["action"] == "allow"
    assert len(rules[0]["features"]) == 1
    feature = list(rules[0]["features"].keys())[0]
    assert rules[0]["features"][feature]
    assert feature in FEATURES_ON or feature in FEATURES_OFF
    return feature in FEATURES_ON


def parse_arguments_jvm_rules(rules):
    assert len(rules) == 1
    assert rules[0]["action"] == "allow"
    if "arch" in rules[0]["os"]:
        assert rules[0]["os"]["arch"] == "x86"
    if "name" in rules[0]["os"]:
        assert rules[0]["os"]["name"] in {"windows", "osx"}
    return False


# returns unsubstituted game arg list and jvm arg list
def compose_args(merged_obj):
    version_obj = merged_obj

    def walker(src, rule_checker, dst):
        for a in src:
            if type(a) is str:
                dst.append(a)
            elif rule_checker(a["rules"]):
                v = a["value"]
                if type(v) is str:
                    dst.append(v)
                else:
                    dst += v

    args_game = []
    args_jvm = []
    walker(version_obj["arguments"]["game"], parse_arguments_game_rules, args_game)
    walker(version_obj["arguments"]["jvm"], parse_arguments_jvm_rules, args_jvm)

    return args_game, args_jvm


def assemble_launch_args(merged_version, main_jar_version, library_map, gamedir: Path, user_credential: dict):
    obj = merged_version
    version = obj["id"]
    assets_dir = (Path(FLAGS.dotmc_folder) / "assets").resolve()
    native_dir = (Path(FLAGS.dotmc_folder) / "versions" / version / "native").resolve()

    args = dict()

    # auth
    args["auth_player_name"] = user_credential["auth_player_name"]
    args["auth_uuid"] = user_credential["auth_uuid"]
    args["auth_access_token"] = user_credential["auth_access_token"]
    args["auth_xuid"] = user_credential["auth_xuid"]

    # misc info
    # forge requires that the version_name to be the official version name
    args["version_name"] = main_jar_version
    args["game_directory"] = str(gamedir.resolve())
    args["assets_root"] = str(assets_dir)
    args["assets_index_name"] = obj["assets"]
    args["user_type"] = "mojang"
    args["version_type"] = obj["type"]
    args["launcher_name"] = "miencraft-launcher"
    args["launcher_version"] = "2.1.17627"
    args["clientid"] = str(uuid.uuid4())    # TODO: what's this?

    # w*h
    args["resolution_width"] = "1920"
    args["resolution_height"] = "1080"

    # natives
    args["natives_directory"] = str(native_dir)

    # forge special
    args["library_directory"] = str(Path(FLAGS.dotmc_folder) / "libraries")
    args["classpath_separator"] = ":"

    # classpath
    jars = []
    for l in library_map.values():
        p = Path(FLAGS.dotmc_folder) / "libraries" / l["downloads"]["artifact"]["path"]
        assert p.is_file(), f"{p} not exists"
        jars.append(str(p.resolve()))

    p = Path(FLAGS.dotmc_folder) / "versions" / main_jar_version / (main_jar_version + ".jar")
    assert p.is_file(), f"{p} not exists"
    jars.append(str(p.resolve()))
    args["classpath"] = ":".join(jars)

    # commandline
    cmds = []
    args_game, args_jvm = compose_args(obj)

    for a in args_jvm:
        for k, v in args.items():
            a = a.replace("${" + k + "}", v)
        cmds.append(a)

    # log4j2 vulnerability hotfix
    cmds.append("-Dlog4j2.formatMsgNoLookups=true")

    cmds.append(obj["mainClass"])
    for a in args_game:
        for k, v in args.items():
            a = a.replace("${" + k + "}", v)
        cmds.append(a)

    return cmds


def main(argv):
    del argv

    # Create an empty launcher_profiles.json to make forge installer happy
    if FLAGS.dotmc_folder is not None:
        Path(FLAGS.dotmc_folder).mkdir(parents=True, exist_ok=True)
        p = Path(FLAGS.dotmc_folder) / "launcher_profiles.json"
        if not p.exists():
            with open(p, "w") as f:
                json.dump(dict(profiles=dict()), f)

    if FLAGS.argfile is None:
        version_manifest = download_version_manifest()
        versions = reversed(version_manifest["versions"])
        for v in versions:
            s = "{type:<9} {id:<12} {url}".format(**v)
            print(s)

    else:
        assert FLAGS.dotmc_folder is not None
        assert FLAGS.version is not None
        assert FLAGS.gamedir is not None

        versions_manifest = download_version_manifest()
        versions_map = {v["id"]: v for v in versions_manifest["versions"]}
        versions_json = download_version_chain(FLAGS.version, versions_map)
        normalized_versions_json = [normalize_version_obj(x) for x in versions_json]

        main_jar_version = download_main_jar(normalized_versions_json)

        merged_version_json = reduce(merge_version_objs, normalized_versions_json)

        library_map, natives_map = build_library_map(merged_version_json)
        download_libraries(list(library_map.values()) + list(natives_map.values()))
        extract_natives(natives_map, FLAGS.version)

        download_asset(merged_version_json)

        # Login
        if FLAGS.offline is not None:
            user_credential = dict(
                auth_player_name=FLAGS.offline,
                auth_uuid="0",
                auth_access_token="0",
                auth_xuid="0",
            )
        else:
            auth_profile = mslogin.get_minecraft_accesstoken(mslogin.load_credential())
            user_credential = dict(
                auth_player_name=auth_profile.auth_player_name,
                auth_uuid=auth_profile.auth_uuid,
                auth_access_token=auth_profile.auth_access_token,
                auth_xuid=auth_profile.auth_xuid,
            )

        # Emit arguments
        launch_args = assemble_launch_args(merged_version_json, main_jar_version, library_map, Path(FLAGS.gamedir),
                                           user_credential)
        with open(FLAGS.argfile, "w") as f:
            for x in launch_args:
                f.write(x + "\n")


if __name__ == '__main__':
    app.run(main)
