import os
import sys
import yaml
import requests
import json
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any
from urllib.parse import urljoin
import hashlib


class VersionChecker:
    def __init__(self, config_path: str):
        self.config = self._load_config(config_path)
        self.github_token = os.environ.get("GITHUB_TOKEN", "")
        self.winget_pkgs_token = os.environ.get("WINGET_PKGS_TOKEN", "")

    def _load_config(self, config_path: str) -> Dict:
        """加载配置文件"""
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _get_latest_version(self, package: Dict) -> Optional[str]:
        """获取上游最新版本"""
        version_source = package["version-source"]
        source_type = version_source.get("type")

        if source_type == "github":
            return self._get_github_version(version_source, package["version-parser"])
        elif source_type == "api":
            return self._get_api_version(version_source, package["version-parser"])
        elif source_type == "webpage":
            return self._get_webpage_version(version_source, package["version-parser"])
        else:
            print(f"Unknown version source type: {source_type}")
            return None

    def _get_github_version(self, source: Dict, parser: Dict) -> Optional[str]:
        """从 GitHub 获取版本"""
        owner = source["owner"]
        repo = source["repo"]

        headers = {}
        if self.github_token:
            headers["Authorization"] = f"token {self.github_token}"

        try:
            # 获取最新 release
            url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.json()
            tag_name = data.get("tag_name", "")

            # 解析版本
            return self._parse_version(tag_name, parser)
        except Exception as e:
            print(f"Error fetching GitHub version for {owner}/{repo}: {e}")
            return None

    def _get_api_version(self, source: Dict, parser: Dict) -> Optional[str]:
        """从 API 获取版本"""
        url = source["url"]
        method = source.get("method", "GET")

        try:
            if method == "GET":
                response = requests.get(url, timeout=30)
            else:
                response = requests.request(method, url, timeout=30)
            response.raise_for_status()
            data = response.json()

            # 使用 JSONPath 解析版本
            path = parser["path"]
            version = self._get_jsonpath_value(data, path)
            return version
        except Exception as e:
            print(f"Error fetching API version from {url}: {e}")
            return None

    def _get_webpage_version(self, source: Dict, parser: Dict) -> Optional[str]:
        """从网页获取版本"""
        url = source["url"]

        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            content = response.text

            # 使用正则解析版本
            pattern = parser["pattern"]
            match = re.search(pattern, content)
            if match:
                return match.group(1)
            return None
        except Exception as e:
            print(f"Error fetching webpage version from {url}: {e}")
            return None

    def _parse_version(self, raw_version: str, parser: Dict) -> Optional[str]:
        """解析版本字符串"""
        parser_type = parser.get("type")

        if parser_type == "regex":
            pattern = parser["pattern"]
            tag_filter = parser.get("tag-filter")

            # 应用标签过滤
            if tag_filter and not re.match(tag_filter, raw_version):
                print(f"Tag {raw_version} does not match filter {tag_filter}")
                return None

            match = re.search(pattern, raw_version)
            if match:
                return match.group(1)
            return None
        elif parser_type == "jsonpath":
            return raw_version

        return raw_version

    def _get_jsonpath_value(self, data: Any, path: str) -> Any:
        """简单的 JSONPath 实现"""
        # 移除前导 $
        path = path.lstrip("$")
        parts = path.split(".")

        value = data
        for part in parts:
            if "[" in part and part.endswith("]"):
                key = part.split("[")[0]
                index = int(part.split("[")[1].rstrip("]"))
                value = value[key][index]
            else:
                value = value[part]

        return value

    def _get_current_winget_version(self, package: Dict) -> Optional[str]:
        """获取当前 winget 版本"""
        winget_id = package["winget-id"]

        try:
            # 将 winget-id 转换为文件路径格式
            # 例如: aome510.spotify-player -> a/aome510/spotify-player
            parts = winget_id.split(".")
            if len(parts) < 2:
                print(f"Invalid winget-id format: {winget_id}")
                return None

            publisher = parts[0]
            package_name = ".".join(parts[1:])
            first_letter = publisher[0].lower()

            # 构建包目录路径
            package_dir = f"manifests/{first_letter}/{publisher}/{package_name}"

            # 使用 GitHub API 获取该目录下的所有版本目录
            headers = {}
            if self.github_token:
                headers["Authorization"] = f"token {self.github_token}"

            url = f"https://api.github.com/repos/microsoft/winget-pkgs/contents/{package_dir}"
            response = requests.get(url, headers=headers, timeout=30)

            if response.status_code == 404:
                print(f"Package {winget_id} not found in winget-pkgs")
                return None

            response.raise_for_status()
            data = response.json()

            # 过滤出目录（版本目录）
            version_dirs = [dir["name"] for dir in data if dir["type"] == "dir"]

            if not version_dirs:
                print(f"No version directories found for {winget_id}")
                return None

            # 找到最新的版本目录（按版本号排序）
            version_dirs.sort(reverse=True)
            latest_version_dir = version_dirs[0]

            # 读取该版本目录的主 manifest 文件
            # 主 manifest 文件名是 {winget-id}.yaml
            manifest_file = f"{winget_id}.yaml"
            manifest_url = f"https://raw.githubusercontent.com/microsoft/winget-pkgs/master/{package_dir}/{latest_version_dir}/{manifest_file}"

            manifest_response = requests.get(manifest_url, headers=headers, timeout=30)

            if manifest_response.status_code == 404:
                # 如果主 manifest 不存在，尝试其他文件
                print(f"Main manifest not found, trying alternatives...")
                return None

            manifest_response.raise_for_status()
            content = manifest_response.text

            # 解析 YAML 获取版本号
            match = re.search(r"PackageVersion:\s*([\d.]+)", content)
            if match:
                return match.group(1)

            return None
        except Exception as e:
            print(f"Error getting current winget version for {winget_id}: {e}")
            return None

    def _compare_versions(self, v1: str, v2: str) -> int:
        """比较两个版本号，返回 1 (v1 > v2), -1 (v1 < v2), 0 (v1 == v2)"""
        # 移除可能的 v 前缀
        v1 = v1.lstrip("v")
        v2 = v2.lstrip("v")

        # 分割版本号
        v1_parts = [int(x) for x in re.findall(r"\d+", v1)]
        v2_parts = [int(x) for x in re.findall(r"\d+", v2)]

        # 补齐长度
        max_len = max(len(v1_parts), len(v2_parts))
        v1_parts.extend([0] * (max_len - len(v1_parts)))
        v2_parts.extend([0] * (max_len - len(v2_parts)))

        # 比较
        for a, b in zip(v1_parts, v2_parts):
            if a > b:
                return 1
            elif a < b:
                return -1

        return 0

    def _generate_komac_command(self, package: Dict, new_version: str) -> List[str]:
        """生成 komac update 命令"""
        winget_id = package["winget-id"]
        installers = package.get("installers", [])

        cmd = [
            "komac",
            "update",
            "--id",
            winget_id,
            "--version",
            new_version,
        ]

        # 添加每个安装包的 URL
        for installer in installers:
            url = installer.get("url")
            url_template = installer.get("url-template")

            # 替换 {version} 占位符
            installer_url = url if url else url_template
            if installer_url and "{version}" in installer_url:
                installer_url = installer_url.replace("{version}", new_version)

            cmd.extend(["--urls", installer_url])

        cmd.append("--submit")

        return cmd

    def _check_installer_urls(self, package: Dict, version: str) -> bool:
        """检查安装包 URL 是否有效"""
        skip_checks = package.get("skip-checks", [])
        if "url-check" in skip_checks:
            return True

        installers = package.get("installers", [])
        for installer in installers:
            url = installer.get("url")
            url_template = installer.get("url-template")

            # 替换 {version} 占位符
            final_url = url if url else url_template
            if final_url and "{version}" in final_url:
                final_url = final_url.replace("{version}", version)

            try:
                response = requests.head(final_url, allow_redirects=True, timeout=10)
                if response.status_code >= 400:
                    print(
                        f"Installer URL check failed: {final_url} (Status: {response.status_code})"
                    )
                    return False
            except Exception as e:
                print(f"Error checking installer URL {final_url}: {e}")
                return False

        return True

    def run_checks(self):
        """运行版本检测"""
        packages = self.config.get("packages", [])
        has_updates = False

        for package in packages:
            pkg_id = package["id"]
            print(f"\nChecking package: {pkg_id}")

            # 获取最新版本
            latest_version = self._get_latest_version(package)
            if not latest_version:
                print(f"Failed to get latest version for {pkg_id}")
                continue

            print(f"Latest version: {latest_version}")

            # 获取当前版本
            current_version = self._get_current_winget_version(package)
            if not current_version:
                print(f"Failed to get current winget version for {pkg_id}")
                continue

            print(f"Current version: {current_version}")

            # 比较版本
            comparison = self._compare_versions(latest_version, current_version)

            if not current_version:
                # 包不存在于 winget-pkgs，需要创建新包
                print(f"Package {package['winget-id']} not found in winget-pkgs")
                print(
                    f"To create a new package, use: komac new --id {package['winget-id']} --version {latest_version}"
                )
                continue

            if comparison > 0:
                print(f"Update available: {current_version} -> {latest_version}")

                # 检查安装包 URL
                if not self._check_installer_urls(package, latest_version):
                    print(f"Skipping update for {pkg_id}: installer URL check failed")
                    continue

                # 生成 komac 命令
                komac_cmd = self._generate_komac_command(package, latest_version)

                # 执行 komac update (komac 会自动创建 PR)
                self._execute_komac_update(komac_cmd)

                has_updates = True
            else:
                print(f"No update needed for {pkg_id}")

        return has_updates

    def _execute_komac_update(self, komac_cmd: List[str]):
        """执行 komac update 命令"""
        print(f"Executing: {' '.join(komac_cmd)}")

        # 设置 komac 需要的环境变量
        env = os.environ.copy()
        if self.winget_pkgs_token:
            env["GITHUB_TOKEN"] = self.winget_pkgs_token

        try:
            result = subprocess.run(
                komac_cmd, capture_output=True, text=True, timeout=300, env=env
            )

            if result.returncode == 0:
                print(f"Successfully executed komac update")
                print(result.stdout)
            else:
                print(f"komac update failed with return code {result.returncode}")
                print(f"STDOUT: {result.stdout}")
                print(f"STDERR: {result.stderr}")
        except subprocess.TimeoutExpired:
            print(f"komac update command timed out")
        except Exception as e:
            print(f"Error executing komac update: {e}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Check for winget package updates")
    parser.add_argument(
        "--config",
        type=str,
        default="config/packages.yaml",
        help="Path to packages configuration file",
    )

    args = parser.parse_args()

    checker = VersionChecker(args.config)
    has_updates = checker.run_checks()

    if has_updates:
        print("\nUpdates found and processed")
        sys.exit(0)
    else:
        print("\nNo updates found")
        sys.exit(0)


if __name__ == "__main__":
    main()
