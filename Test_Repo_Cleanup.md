## To delete only untracked files and folders while preserving .git, commits, and tracked files

Preview what it will delete first:

```Shell
git clean -nd
```

Run:

```Shell
python -c "import subprocess; subprocess.run(['git','clean','-fd'], check=True); subprocess.run(['git','status'], check=True)"
```

This does not affect tracked files marked D. To restore those deleted tracked files instead, run:

```Shell
git restore --source=HEAD --staged --worktree .
```

## To completely delete/renew the repo:

```Shell
python -c "import os, stat, shutil, subprocess; from pathlib import Path; root=Path.cwd().resolve(); assert (root/'.git').exists(), 'No .git entry found; verify that this is the test repository'; exec('def force_remove(func, path, exc):\n    os.chmod(path, stat.S_IWRITE)\n    func(path)'); [(shutil.rmtree(p, onexc=force_remove) if p.is_dir() and not p.is_symlink() else (os.chmod(p, stat.S_IWRITE), p.unlink())) for p in list(root.iterdir())]; subprocess.run(['git','init','-b','main'], check=True); subprocess.run(['git','commit','--allow-empty','-m','Initial empty commit'], check=True); subprocess.run(['git','status'], check=True)"
```

## To completely remove/delete the worktrees `.bhai-worktrees`:

```Shell
Remove-Item -LiteralPath "C:\Users\LOQ\Desktop\Projects\.bhai-worktrees" -Recurse -Force
```