#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import errno
import shutil
import logging
import argparse
from typing import Optional

from common import (
    config as c,
    pb,
    get_cmd_or_die,
    download_archive,
    die,
    est_parallel_link_jobs,
    invoke,
    install_sig,
    ensure_dir,
    on_x86,
    on_mac,
    setup_logging,
    have_rust_toolchain,
    ensure_clang_version,
    git_ignore_dir,
    on_linux,
    get_ninja_build_type,
)


def download_llvm_sources():
    tar = get_cmd_or_die("tar")

    # make sure we have the gpg public key installed first
    install_sig(c.LLVM_PUBKEY)

    with pb.local.cwd(c.DEPS_DIR):
        # download archives and signatures
        for (aurl, asig, afile, _) in zip(
                c.LLVM_ARCHIVE_URLS,
                c.LLVM_SIGNATURE_URLS,
                c.LLVM_ARCHIVE_FILES,
                c.LLVM_ARCHIVE_DIRS):

            # download archive + signature
            download_archive(aurl, afile, asig)

    # first extract llvm archive
    if not os.path.isdir(c.LLVM_SRC):
        logging.info("extracting %s", c.LLVM_ARCHIVE_FILES[0])
        tar("xf", c.LLVM_ARCHIVE_FILES[0])
        os.rename(c.LLVM_ARCHIVE_DIRS[0], c.LLVM_SRC)

    # then clang front end
    with pb.local.cwd(os.path.join(c.LLVM_SRC, "tools")):
        if not os.path.isdir("clang"):
            logging.info("extracting %s", c.LLVM_ARCHIVE_FILES[1])
            tar("xf", os.path.join(c.ROOT_DIR, c.LLVM_ARCHIVE_FILES[1]))
            os.rename(c.LLVM_ARCHIVE_DIRS[1], "clang")

        with pb.local.cwd("clang/tools"):
            if not os.path.isdir("extra"):
                logging.info("extracting %s", c.LLVM_ARCHIVE_FILES[2])
                tar("xf", os.path.join(c.ROOT_DIR, c.LLVM_ARCHIVE_FILES[2]))
                os.rename(c.LLVM_ARCHIVE_DIRS[2], "extra")


def configure_and_build_llvm(args) -> None:
    """
    run cmake as needed to generate ninja buildfiles. then run ninja.
    """
    # Possible values are Release, Debug, RelWithDebInfo and MinSizeRel
    build_type = "Debug" if args.debug else "RelWithDebInfo"
    ninja_build_file = os.path.join(c.LLVM_BLD, "build.ninja")
    with pb.local.cwd(c.LLVM_BLD):
        if os.path.isfile(ninja_build_file) and not args.xcode:
            prev_build_type = get_ninja_build_type(ninja_build_file)
            run_cmake = prev_build_type != build_type
        else:
            run_cmake = True

        if run_cmake:
            cmake = get_cmd_or_die("cmake")
            max_link_jobs = est_parallel_link_jobs()
            assertions = "1" if args.assertions else "0"
            ast_ext_dir = "-DLLVM_EXTERNAL_C2RUST_AST_EXPORTER_SOURCE_DIR={}"
            ast_ext_dir = ast_ext_dir.format(c.AST_EXPO_SRC_DIR)
            cargs = ["-G", "Ninja", c.LLVM_SRC,
                     "-Wno-dev",
                     "-DCMAKE_C_COMPILER=clang",
                     "-DCMAKE_CXX_COMPILER=clang++",
                     "-DCMAKE_INSTALL_PREFIX=" + c.LLVM_INSTALL,
                     "-DCMAKE_BUILD_TYPE=" + build_type,
                     "-DLLVM_PARALLEL_LINK_JOBS={}".format(max_link_jobs),
                     "-DLLVM_ENABLE_ASSERTIONS=" + assertions,
                     "-DCMAKE_EXPORT_COMPILE_COMMANDS=1",
                     "-DLLVM_EXTERNAL_PROJECTS=c2rust-ast-exporter",
                     ast_ext_dir]

            if on_x86():  # speed up builds on x86 hosts
                cargs.append("-DLLVM_TARGETS_TO_BUILD=X86")
            invoke(cmake[cargs])

            # NOTE: we only generate Xcode project files for IDE support
            # and don't build with them since the cargo build.rs files
            # rely on cmake to build native code.
            if args.xcode:
                cargs[1] = "Xcode"
                # output Xcode project files in a separate dir
                with pb.local.cwd(c.AST_EXPO_PRJ_DIR):
                    invoke(cmake[cargs])
        else:
            logging.debug("found existing ninja.build, not running cmake")

        # if args.xcode:
        #     xcodebuild = get_cmd_or_die("xcodebuild")
        #     xc_conf_args = ['-configuration', build_type]
        #     xc_args = xc_conf_args + ['-target', 'llvm-config']
        #     invoke(xcodebuild, *xc_args)
        #     xc_args = xc_conf_args + ['-target', 'c2rust-ast-exporter']
        #     invoke(xcodebuild, *xc_args)
        ninja = get_cmd_or_die("ninja")
        ninja_args = ['c2rust-ast-exporter', 'llvm-config', 'install-clang-headers']
        invoke(ninja, *ninja_args)


def build_transpiler(args):
    cargo = get_cmd_or_die("cargo")
    build_flags = ["build", "--features", "llvm-static"]

    if not args.debug:
        build_flags.append("--release")

    llvm_config = os.path.join(c.LLVM_BLD, "bin/llvm-config")
    assert os.path.isfile(llvm_config), "missing binary: " + llvm_config

    # HACK(perl): `llvm-config --system-libs` does what we want but
    # doesn't seem to work unless we actually install our LLVM build.
    if on_mac():
        llvm_system_libs = "-lz -lcurses -lm -lxml2"
    else:  # linux
        llvm_system_libs = "-lz -lrt -ltinfo -ldl -lpthread -lm -lxml2"

    with pb.local.cwd(os.path.join(c.ROOT_DIR, "c2rust-transpile")):
        with pb.local.env(LLVM_CONFIG_PATH=llvm_config,
                          LLVM_SYSTEM_LIBS=llvm_system_libs):
            # build with custom rust toolchain
            invoke(cargo, "+" + c.CUSTOM_RUST_NAME, *build_flags)


def _parse_args():
    """
    define and parse command line arguments here.
    """
    desc = 'download dependencies for the AST exporter and built it.'
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument('-c', '--clean-all', default=False,
                        action='store_true', dest='clean_all',
                        help='clean everything before building')
    parser.add_argument('--without-assertions', default=True,
                        action='store_false', dest='assertions',
                        help='build the tool and clang without assertions')
    parser.add_argument('-x', '--xcode', default=False,
                        action='store_true', dest='xcode',
                        help='generate Xcode project files (macOS only)')
    c.add_args(parser)
    args = parser.parse_args()

    if not on_mac() and args.xcode:
        die("-x/--xcode option requires macOS host.")

    c.update_args(args)
    return args


def binary_in_path(binary_name) -> bool:
    try:
        # raises CommandNotFound exception if not available.
        _ = pb.local[binary_name]  # noqa: F841
        return True
    except pb.CommandNotFound:
        return False


def _main():
    setup_logging()
    logging.debug("args: %s", " ".join(sys.argv))

    # FIXME: allow env/cli override of LLVM_SRC, LLVM_VER, and LLVM_BLD
    # FIXME: check that cmake and ninja are installed
    # FIXME: option to build LLVM/Clang from master?

    # earlier plumbum versions are missing features such as TEE
    if pb.__version__ < c.MIN_PLUMBUM_VERSION:
        err = "locally installed version {} of plumbum is too old.\n" \
            .format(pb.__version__)
        err += "please upgrade plumbum to version {} or later." \
            .format(c.MIN_PLUMBUM_VERSION)
        die(err)

    args = _parse_args()

    # prerequisites
    if not have_rust_toolchain(c.CUSTOM_RUST_NAME):
        die("missing rust toolchain: " + c.CUSTOM_RUST_NAME, errno.ENOENT)

    # clang 3.6.0 is known to work; 3.4.0 known to not work.
    ensure_clang_version([3, 6, 0])
    # NOTE: it seems safe to disable this check since we now
    # that we use a rust-toolchain file for rustc versioning.
    # ensure_rustc_version(c.CUSTOM_RUST_RUSTC_VERSION)

    if args.clean_all:
        logging.info("cleaning all dependencies and previous built files")
        shutil.rmtree(c.LLVM_SRC, ignore_errors=True)
        shutil.rmtree(c.LLVM_BLD, ignore_errors=True)
        shutil.rmtree(c.DEPS_DIR, ignore_errors=True)
        shutil.rmtree(c.AST_EXPO_PRJ_DIR, ignore_errors=True)
        cargo = get_cmd_or_die("cargo")
        with pb.local.cwd(os.path.join(c.ROOT_DIR, "c2rust-transpile")):
            invoke(cargo, "clean")

    ensure_dir(c.LLVM_BLD)
    ensure_dir(c.DEPS_DIR)
    git_ignore_dir(c.DEPS_DIR)

    download_llvm_sources()

    configure_and_build_llvm(args)

    build_transpiler(args)


if __name__ == "__main__":
    _main()
