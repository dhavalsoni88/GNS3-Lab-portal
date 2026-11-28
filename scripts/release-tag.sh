#!/usr/bin/env bash
# Create the git tag and GitHub release for the version in pyproject.toml, once.
#
# Run by .github/workflows/release.yml on every push to main. It does nothing
# when the tag already exists, so pushes that do not bump the version are free.
# The release notes are the matching CHANGELOG.md section.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)
[[ -n "$version" ]] || { echo "No version found in pyproject.toml." >&2; exit 1; }
tag="v$version"

if git ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null 2>&1; then
  echo "$tag already exists: nothing to do."
  exit 0
fi
if grep -q "^## $tag — Unreleased" CHANGELOG.md; then
  echo "::warning::CHANGELOG.md still lists $tag as Unreleased, so it was not released. Date the heading and run this workflow again."
  exit 0
fi

section=$(mktemp)
awk -v heading="## $tag " 'index($0, heading) == 1 { found = 1; next } /^## v/ { found = 0 } found' CHANGELOG.md > "$section"
[[ -s "$section" ]] || { echo "CHANGELOG.md has no section headed '## $tag — <date>'." >&2; exit 1; }

notes=$(mktemp)
{
  printf '### Upgrade\nTake a server snapshot first, then:\n\n'
  printf '    sudo git -C /opt/gns3-lab-portal pull --ff-only\n'
  printf '    sudo bash /opt/gns3-lab-portal/scripts/upgrade.sh\n'
  cat "$section"
} > "$notes"

flags=()
[[ "$version" =~ ^[0-9]+(\.[0-9]+)*$ ]] || flags+=(--prerelease)   # 9.2.0rc1 and the like
gh release create "$tag" --target "${GITHUB_SHA:-$(git rev-parse HEAD)}" --title "$tag" --notes-file "$notes" "${flags[@]}"
echo "Released $tag."
