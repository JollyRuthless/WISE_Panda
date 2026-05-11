# GitHub Commands

## Check where you are

```powershell
git status
git branch --show-current
git remote -v
```

## Fetch the latest remote info

```powershell
git fetch origin
```

## See what changed on the remote

```powershell
git status -sb
git log --oneline --decorate --graph HEAD..origin/main
```

## Pull the latest changes onto your current branch

```powershell
git pull --ff-only
```

## Stage your work

```powershell
git add .
```

Or stage specific files:

```powershell
git add path/to/file
```

## Commit your work

```powershell
git commit -m "Short description of the change"
```

## Push your current branch

```powershell
git push
```

If this is the first push for a new branch:

```powershell
git push -u origin HEAD
```

## Create a new branch

```powershell
git checkout -b your-branch-name
```

## Switch to an existing branch

```powershell
git checkout branch-name
```

## Update `main`

```powershell
git checkout main
git fetch origin
git pull --ff-only origin main
```

## Start a new branch from updated `main`

```powershell
git checkout main
git fetch origin
git pull --ff-only origin main
git checkout -b your-new-branch-name
```

## Push a branch after changes

```powershell
git status
git add .
git commit -m "Describe the work"
git push -u origin HEAD
```

## Useful read-only checks

```powershell
git diff
git diff --staged
git log --oneline --decorate --graph -20
```

## Safe everyday flow

```powershell
git status
git fetch origin
git pull --ff-only
git add .
git commit -m "Describe the work"
git push
```

## Notes

- Use `git fetch origin` when you want to refresh remote information without changing your local files.
- Use `git pull --ff-only` when you want the current branch updated safely without creating a merge commit.
- Use `git push -u origin HEAD` the first time you push a new branch.
- Run `git status` often. It is the best quick check for what Git thinks is happening.
