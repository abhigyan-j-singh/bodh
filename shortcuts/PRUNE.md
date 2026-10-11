# The Prune button

One Apple Shortcut, named **Prune**, runs `bodh prune` on the Mac that runs bodh, over SSH. It works from the
iPhone home screen, the Mac menu bar, the Apple Watch and Siri ("Hey Siri, Prune"). Nothing new listens
on the network: it uses the SSH you already have through Tailscale.

## Once, on the Mac

1. System Settings → General → Sharing → turn on **Remote Login** (SSH). Allow access for your user only.
2. Tailscale is installed and signed in; note the machine name (for example `my-mac`), reachable as
   `my-mac` or `my-mac.<tailnet>.ts.net` from your other devices.
3. Make sure `~/bodh/.venv/bin/bodh prune --dry-run --yes` works from a terminal (`~/bodh` being
   wherever you cloned it).

## Build the Shortcut (iPhone or Mac; it syncs to both)

1. Shortcuts → **+** → name it **Prune**.
2. Add the action **Run Script Over SSH** (search "SSH").
   - Host: `my-mac` (your Tailscale name), Port 22
   - User: your macOS user name
   - Authentication: **SSH Key** (tap to generate one; then on the Mac run
     `echo '<the public key Shortcuts shows>' >> ~/.ssh/authorized_keys`), or Password if you prefer
   - Input: none
   - Script:

     ```sh
     cd "$HOME/bodh" && .venv/bin/bodh prune --yes 2>&1 | tail -n 4
     ```

3. Add **Show Notification** (or **Show Result**) with the SSH output as its text.
4. Run it once by hand; approve the SSH connection prompt.
5. Add to Home Screen (share sheet → Add to Home Screen) and, on the Mac, pin it to the menu bar
   (Shortcut details → "Pin in Menu Bar").

## Variants

Duplicate the Shortcut and change the script line for other depths:

- `… bodh prune --fraction 0.3 --yes …` cuts the worst 30% of what is unread, each time you press it
- `… bodh prune --below 65 --yes …` cuts everything scoring under 65
- `… bodh prune --dry-run …` shows the cut without making it
- `… bodh undo …` reverses the last cut

## Without a Shortcut

Any BazQux client works as a trigger too: add the tag `prune` to any article in BazQux and the next
timer run prunes one step to the target, then removes the tag. And plain `ssh my-mac` followed by
`bodh prune` is always there.
