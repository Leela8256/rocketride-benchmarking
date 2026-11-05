# AWS box access

Everything about getting onto the benchmark box, running things on it, and
getting results back. Read it once end to end; the traps at the bottom have
each cost at least one run.

## 1. The box

| item | value |
|---|---|
| instance | `i-0bdc8b1e18f2a5348`, **c7i.8xlarge**: 32 vCPU (16 physical cores, SMT pairs 0-15 / 16-31), 61 GB RAM, x86_64 |
| disk | 1 TB gp3, about 900 GB free after corpora |
| RAM tmpfs | `/dev/shm` about 30 GB (used by the wave runner, idle otherwise) |
| region / zone | `us-east-1`, us-east-1a |
| OS | Ubuntu 22.04, Docker 29.x with the compose plugin, cgroup v2, no `aws` CLI preinstalled |
| user | you land as `ssm-user`; docker works without sudo; `sudo` works for symlinks and the like |
| AWS account | `250017478984` (named `rocketride-dev` in SSO; it is the real production account, so treat everything outside this instance as untouchable) |
| permission set | `BenchmarkBoxOperator` (role `AWSReservedSSO_BenchmarkBoxOperator_*`) |
| SSO start URL | `https://ssoins-7223225ee069e552.portal.us-east-1.app.aws/#/`, SSO region `us-east-1` |
| S3 bucket | `s3://rocketride-benchmark-data/` ; our corpora and results live under the `leela/` prefix |
| cost | about $1.43 per hour while running. The box auto-stops after roughly an hour below 20 % CPU, but do not rely on it |

The box exists for exactly one purpose, framework benchmarking, and the
permission set is scoped to instances tagged `Purpose=framework-benchmark`.

## 2. What the permission set allows, and what it does not

```
laptop --(AWS API: ec2 start/stop/describe, ssm start-session)--> AWS control plane
laptop --(SSM session: keystrokes over TLS)----------------------> box shell (ssm-user)
box    --(aws s3 cp, instance role)------------------------------> S3   <- the ONLY way results leave
box    --(git clone / pull over HTTPS)<--------------------------- GitHub <- the ONLY way code arrives
```

Allowed: interactive SSM session with the default shell document; EC2
start, stop and describe on the tagged instance; S3 list and get on the
bucket from the laptop (verified on the `leela/` prefix; check your own
grant with `aws s3 ls`).

Denied, verified, do not burn time on them: `ssm:SendCommand`,
`AWS-StartNonInteractiveCommand`, `AWS-StartInteractiveCommand`,
`ssm:TerminateSession`, SSH, SSH over SSM, port forwarding, scp, CloudWatch
reads, S3 put from the laptop.

Consequences:

- You can never curl a box port from the laptop. Every health check and every
  driver runs on the box.
- Code reaches the box only by `git clone` of this repository. If the
  repository is private, the clone needs a token:
  `git clone https://<github-token>@github.com/Leela8256/rocketride-benchmarking.git`.
- Data reaches the box only by download on the box (corpora come from their
  public sources or from the S3 copies already staged).
- Results leave only via `aws s3 cp` from the box, using the instance role,
  which never expires.

## 3. Laptop setup (once)

1. Install the AWS CLI v2 (`brew install awscli`) and the Session Manager
   plugin. The Homebrew cask for the plugin needs sudo; if that is a problem,
   download the plugin bundle from AWS and place the binary at
   `/opt/homebrew/bin/session-manager-plugin`.
2. Ask the AWS admin to add you to the `BenchmarkBoxOperator` permission set.
   Until that is done `aws sso login` ends with "No AWS accounts are available
   to you".
3. Configure a profile:

   ```bash
   aws configure sso
   #   SSO session name:  benchmark
   #   SSO start URL:     https://ssoins-7223225ee069e552.portal.us-east-1.app.aws/#/
   #   SSO region:        us-east-1
   #   account:           250017478984
   #   role:              BenchmarkBoxOperator
   #   default region:    us-east-1
   #   profile name:      <your-profile-name>
   aws sso login --profile <your-profile-name>
   aws sts get-caller-identity --profile <your-profile-name>
   ```

4. Copy `local/local.env.example` to `local/local.env` and set
   `BOX_PROFILE=<your-profile-name>`. `local/box.sh` reads that file.

The SSO token expires after a few hours. When a `box.sh` command fails with
"Token has expired", run `bash local/box.sh login` again. Runs on the box are
unaffected: they use the instance role.

## 4. `local/box.sh`, the only laptop command

```
bash local/box.sh login                  SSO login (opens the browser, you approve)
bash local/box.sh status                 instance state + SSM agent state
bash local/box.sh start                  start the instance, wait until the SSM agent is Online
bash local/box.sh stop                   stop the instance (disk survives)
bash local/box.sh shell                  interactive shell (for humans; 20 min idle timeout)
bash local/box.sh run '<cmd>'            one command, output returned, exit code preserved
bash local/box.sh launch <name> '<cmd>'  long run: nohup on the box, log in ~/logs/<name>.log
bash local/box.sh tail <name> [lines]    tail that log
bash local/box.sh ps                     launched jobs still running
```

How `run` works: it pipes the command and an `exit` into a normal SSM session,
under a pseudo-terminal, and appends an `__RC=<n>` marker so the remote exit
code survives. The pseudo-terminal is mandatory: since the SSM agent updated
to 3.3.4793.0 a session without a TTY dies instantly with
`Cannot perform start session: EOF` and stays "Connected" server-side.
Twenty-five such zombies hit the per-instance session cap and lock everyone
out until a stop/start or the idle timeout clears them. The script uses the
macOS `script -q /dev/null <cmd>` form; on a Linux laptop change it to
`script -q -c "<cmd>" /dev/null`.

Rules for `run`:

- Keep each command under ten minutes. Longer work goes through `launch` and
  `tail`.
- Never put a marker string you intend to grep for inside the remote command:
  the pseudo-terminal echoes the command back, and the echo matches your
  grep. Grep on the plain `tail` output instead.
- Quote the whole command in single quotes; use `$HOME` and `~` freely, they
  expand on the box.

## 5. Day one on the box

```bash
bash local/box.sh login
bash local/box.sh start
bash local/box.sh run 'whoami && uname -m && nproc && df -h / | tail -1'
bash local/box.sh run 'git clone https://github.com/Leela8256/rocketride-benchmarking.git'
bash local/box.sh run 'cd rocketride-benchmarking/aws_pdf && bash run/preflight.sh'
bash local/box.sh run 'cd rocketride-benchmarking/aws_pdf && bash run/install_awscli.sh'
bash local/box.sh run 'sudo ln -sf /home/ssm-user/.local/bin/aws /usr/local/bin/aws && aws --version'
bash local/box.sh run 'aws sts get-caller-identity && aws s3 ls s3://rocketride-benchmark-data/leela/'
```

`preflight.sh` is read-only and checks architecture, cores, memory, disk,
docker, compose, git, python3, curl and network reachability. The
`install_awscli.sh` step exists because the Ubuntu image ships no AWS CLI;
the installer puts it in `~/.local/bin`, which non-login shells (nohup) do
not see, hence the symlink into `/usr/local/bin`. Without that symlink a
detached run silently skips its S3 upload with "no aws cli/role". Re-check
the symlink after any box change.

If the repository was already cloned (by someone else, on the shared box),
`git -C ~/rocketride-benchmarking pull --ff-only` is the update path. Never
edit code on the box: fix on the laptop, commit, push, pull on the box. The
clone is disposable; S3 holds the results.

## 6. Long runs

Never leave a long run inside a live session. The pattern is:

```bash
bash local/box.sh launch <name> 'cd ~/rocketride-benchmarking/<track> && <env> bash run/<runner>.sh'
bash local/box.sh tail <name> 60          # any time later
bash local/box.sh ps
```

`launch` runs `nohup bash -c '<cmd>' > ~/logs/<name>.log 2>&1 &` on the box.
It survives the session closing and the laptop sleeping. It does not survive
the box stopping itself: the box auto-stops after about an hour below 20 %
CPU. A measured run keeps CPU up, so that is only a risk during long
low-CPU phases (corpus extraction, hashing, image builds, report writing).
The campaign runners carry a keepalive busy loop for those phases and pause
it (SIGSTOP) during measured spans. Never add a keepalive of your own inside
a measured span: it contaminates the CPU numbers.

If the box stops mid-run, nothing on disk is lost. `bash local/box.sh start`,
inspect the log, and either resume (the wave and campaign runners are
resumable) or relaunch.

## 7. Results: S3 layout

| what | where |
|---|---|
| PDF runs | `s3://rocketride-benchmark-data/leela/bench/<run-tag-or-stamp>/` |
| video runs | `s3://rocketride-benchmark-data/leela/videobench/<run>-<stamp>/` |
| staged corpora | `s3://rocketride-benchmark-data/leela/corpus/<set>/` (`ami_full`, `ami30h`, `ami30test`, `archive_films_v2`) |

PDF runs upload once at the end (`matched_run.sh`), video runs sync every
60 s while running, so an interruption loses at most a minute of records.
The PDF prefix is overridable with `BENCH_S3`; several video runners accept
`S3_DEST`, the main ones (`native170.sh`, `matched8x4_ami.sh`) write under
`leela/videobench/` by construction. If you are asked to keep your results
under your own prefix, confirm first that the instance role may write there.

Pull a run to the laptop and re-derive everything:

```bash
aws s3 cp --recursive s3://rocketride-benchmark-data/leela/bench/<run>/ ./run --profile <your-profile>
python3 aws_pdf/bench/report.py ./run
```

## 8. Stopping

```bash
bash local/box.sh stop
```

Do it every time. Disk, images, model caches and corpora all survive a stop.
Only the idle session zombies and the engine's container scratch are cleared.
