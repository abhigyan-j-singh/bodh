# The Prune button

One Apple Shortcut, named **Prune**, runs `viveka prune` on the Mac mini over SSH. It works from the
iPhone home screen, the Mac menu bar, the Apple Watch and Siri ("Hey Siri, Prune"). Nothing new listens
on the network: it uses the SSH you already have through Tailscale.

## Once, on the Mac mini

1. System Settings → General → Sharing → turn on **Remote Login** (SSH). Allow access for your user only.
2. Tailscale is installed and signed in; note the machine name (for example `mac-mini`), reachable as
   `mac-mini` or `mac-mini.<tailnet>.ts.net` from your other devices.
3. Make sure `~/Documents/RSS AI Teacher/viveka/.venv/bin/viveka prune --dry-run` works from a terminal.

## Build the Shortcut (iPhone or Mac; it syncs to both)

1. Shortcuts → **+** → name it **Prune**.
2. Add the action **Run Script Over SSH** (search "SSH").
   - Host: `mac-mini` (your Tailscale name), Port 22
   - User: `abhigyan`
   - Authentication: **SSH Key** (tap to generate one; then on the Mac mini run
     `echo '<the public key Shortcuts shows>' >> ~/.ssh/authorized_keys`) — or Password if you prefer
   - Input: none
   - Script:

     ```sh
     cd "$HOME/Documents/RSS AI Teacher/viveka" && .venv/bin/viveka prune 2>&1 | tail -n 4
     ```

3. Add **Show Notification** (or **Show Result**) with the SSH output as its text.
4. Run it once by hand; approve the SSH connection prompt.
5. Add to Home Screen (share sheet → Add to Home Screen) and, on the Mac, pin it to the menu bar
   (Shortcut details → "Pin in Menu Bar").

## Variants

Duplicate the Shortcut and change the script line for other depths:

- `… viveka prune --fraction 0.3 …` cuts the worst 30% of what is unread, each time you press it
- `… viveka prune --below 65 …` cuts everything scoring under 65
- `… viveka prune --dry-run …` shows the cut without making it
- `… viveka undo …` reverses the last cut

## Without a Shortcut

Any BazQux client works as a trigger too: add the tag `prune` to any article in BazQux and the next
timer run prunes one step to the target, then removes the tag. And plain `ssh mac-mini` followed by
`viveka prune` is always there.
