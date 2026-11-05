#!/usr/bin/env bash
# Install AWS CLI v2 into the user's home. Runs ON the box.
#
# WHY THIS EXISTS: the box is an Ubuntu AMI, and Canonical's images do not
# ship the AWS CLI (Amazon Linux does). There is no sudo, so apt/snap are
# out. The official installer supports a user-dir install, which is enough
# for `aws s3 cp` via the instance role.
#
# python3's zipfile is used instead of unzip (also absent, also no sudo) --
# note extractall() drops the executable bit, hence the chmod.
#
#   bash run/install_awscli.sh
set -euo pipefail
ZIP=/tmp/awscliv2.zip
command -v aws >/dev/null 2>&1 && { echo "already installed: $(aws --version)"; exit 0; }
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "$ZIP"
python3 -c "import zipfile;zipfile.ZipFile('$ZIP').extractall('/tmp/awscli')"
chmod -R u+x /tmp/awscli/aws
/tmp/awscli/aws/install -i "$HOME/.local/aws-cli" -b "$HOME/.local/bin"
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
  grep -q 'local/bin' "$rc" 2>/dev/null || echo 'export PATH=$HOME/.local/bin:$PATH' >> "$rc"
done
export PATH="$HOME/.local/bin:$PATH"
aws --version
aws sts get-caller-identity --output json
