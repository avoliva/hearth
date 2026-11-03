# The data disk

By default LXD keeps the storage pool as a loop file on the root disk, and `hostctl doctor` refuses
a pool size the root disk could not hold while keeping a reserve free. A second disk (or partition)
gives the pool room without risking the root disk filling up.

## Configure it

1. Make an ext4 filesystem on the partition you want to use (`sudo mkfs.ext4 -L data /dev/<part>`).
   hostctl never partitions or formats disks.
2. Find its filesystem UUID: `lsblk -f` or `sudo blkid /dev/<part>`.
3. Add it to `config/site.toml`:

   ```toml
   [data_disk]
   uuid = "<filesystem uuid>"
   mount = "/srv/data"
   lxd_disks = true   # keep LXD's loop files (the pool) on this disk
   ```

4. `hostctl plan` shows the `data-disk` component mounting it at boot by UUID. With
   `lxd_disks = true` it also bind-mounts a folder of the disk over LXD's loop-file directory, and
   makes LXD's daemon require that mount, so LXD never starts from an empty directory if the disk
   is missing.

## A new server

On a server with no pool yet, apply once: the disk is mounted first, then the pool is created on it.

## Moving an existing pool

The bind mount is only made over an empty directory, so hostctl never hides a live pool from LXD.
While the pool's file is still on the root disk, the plan warns and changes nothing there. To move
it (every instance is offline for a few minutes):

1. `hostctl apply --yes --only data-disk` mounts the disk (the bind mount still waits).
2. Note which instances run (`sudo lxc list --all-projects`), stop them all, then stop LXD
   (`sudo snap stop lxd`).
3. Move the pool's file out of `/var/snap/lxd/common/lxd/disks/` to a rollback folder, so the
   directory is empty.
4. `hostctl apply --yes --only data-disk` again: now it writes the bind mount and LXD's drop-in and
   mounts the data disk's folder over LXD's directory.
5. Copy the pool's file back into that directory, keeping it sparse
   (`sudo cp --sparse=always --preserve=mode,ownership,timestamps ...`), compare it with `cmp`,
   then `sudo snap start lxd` and start the instances that were running.
6. `hostctl doctor`, `hostctl plan`, `hostctl apply --yes` (grow `lxd.pool.size` now if you want
   the room), then check `plan` is empty and `verify` passes. Delete the rollback copy by hand once
   you are happy.
