#!/usr/bin/env bash
# Box preflight — first thing to run on the AWS benchmark box (checklist 1.4).
# Read-only: checks the box is what we think it is before anything is installed.
# Usage: bash preflight.sh   (run from aws_pdf/)
set -u

pass=0; fail=0
ok()   { pass=$((pass+1)); printf 'PASS  %s\n' "$1"; }
bad()  { fail=$((fail+1)); printf 'FAIL  %s\n' "$1"; }
info() { printf 'INFO  %s\n' "$1"; }

echo "== prod-bench AWS preflight — $(date -u +%Y-%m-%dT%H:%M:%SZ) =="

# Identity / platform
arch=$(uname -m)
[ "$arch" = "x86_64" ] && ok "arch: $arch" || bad "arch: $arch (expected x86_64 — timings invalid otherwise)"
cores=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)
[ "$cores" -eq 32 ] && ok "cores: $cores" || bad "cores: $cores (expected 32 on c7i.8xlarge — LG executor width depends on this)"
mem_gb=$(awk '/MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo)
[ "$mem_gb" -ge 55 ] && ok "memory: ${mem_gb} GB" || bad "memory: ${mem_gb} GB (expected ~61)"
info "kernel: $(uname -r)"
info "user: $(whoami) (expect ssm-user)"

# Disk — datasets (0.7 GB) + images + raw results need room
avail_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[ "$avail_gb" -ge 40 ] && ok "disk free on /: ${avail_gb} GB" || bad "disk free on /: ${avail_gb} GB (<40 GB — clean up before running)"

# Toolchain
if docker version >/dev/null 2>&1; then
  ok "docker daemon reachable without sudo ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"
else
  bad "docker not usable as $(whoami)"
fi
if docker compose version >/dev/null 2>&1; then
  ok "docker compose: $(docker compose version --short 2>/dev/null)"
else
  bad "docker compose plugin missing"
fi
command -v git >/dev/null 2>&1 && ok "git: $(git --version | awk '{print $3}')" || bad "git missing"
command -v python3 >/dev/null 2>&1 && ok "python3: $(python3 -V 2>&1 | awk '{print $2}')" || bad "python3 missing"
command -v aws >/dev/null 2>&1 && ok "aws cli: $(aws --version 2>&1 | awk '{print $1}')" \
  || info "aws cli absent — install with run/install_awscli.sh (expected on this AMI)"
command -v curl >/dev/null 2>&1 && ok "curl present" || bad "curl missing (corpus download)"
command -v unzip >/dev/null 2>&1 && ok "unzip present" \
  || info "unzip absent — corpus fetcher uses python3 zipfile by design"

# cgroup version — the samplers read container CPU/RSS from cgroups
if [ -f /sys/fs/cgroup/cgroup.controllers ]; then info "cgroup: v2"; else info "cgroup: v1"; fi

# Network reachability (HTTPS only, nothing downloaded)
for url in https://github.com https://registry-1.docker.io/v2/ https://s3.us-east-1.amazonaws.com; do
  if curl -sI --max-time 10 -o /dev/null "$url"; then ok "reachable: $url"; else bad "unreachable: $url"; fi
done

# Instance role — determines the S3 exfil path (checklist 1.5)
if aws sts get-caller-identity >/dev/null 2>&1; then
  ok "instance role usable: $(aws sts get-caller-identity --query Arn --output text)"
else
  bad "aws sts get-caller-identity failed — no instance role? S3 exfil blocked"
fi

echo "== result: ${pass} pass, ${fail} fail =="
[ "$fail" -eq 0 ]
