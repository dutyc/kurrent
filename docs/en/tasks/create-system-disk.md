# Create a System Disk from a Master Image

This task describes how to deploy a customized master image to a storage node and create a system disk for a Worker.

A system disk is created by cloning a master image. The master image is built once in a virtual machine or offline environment and placed in the storage node's disk directory; the control plane clones a system disk per Worker and issues that disk's credentials at creation time. The master image is only a clone source and never participates in boot.

## Before you begin

- The control plane is deployed and at least one storage node has joined. See [Deployment](../setup/deployment.md).
- The master image has been built and verified per [Customize a Master Image](./system-disk.md): 4K-native GPT, GRUB 2.14, kernel NVMe authentication and TLS, and an initramfs that consumes NBFT/NBCT. The master image uses a [verified distribution](../reference/distributions.md).
- The target storage node's declaration sets `spec.agent.diskDir`, and the filesystem under that directory supports reflink.
- The target Worker already exists.

## Deploy the master image to the storage node

1. Shut down the master-image VM and make sure the image is flushed to disk.
2. Copy the whole-disk image (GPT, ESP and root partition) to the target storage node's `spec.agent.diskDir` with a filename containing `_tpl_` (case-insensitive):

   ```text
   /pool1/disk_dir/_tpl_debian12.img
   ```

3. Wait for the Agent's background scan. The master image appears under `/masters` or in the WebUI master-image list, and its `gpt` should be `4k`.

## Create the system disk

1. Create the system disk on the WebUI Worker detail page, or call the create endpoint:

   ```bash
   curl -s -X POST "$BASE_URL/workers/worker-01/luns/disk" \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "type": "master",
       "os": "ubuntu",
       "os_version": "22.04",
       "name": "_tpl_ubuntu_2204.img"
     }'
   ```

2. The control plane picks the placement node, generates the disk-level identifier `os_tag` and the disk NQN; the Agent clones the master image with reflink and issues that disk's DHHC-1 credential.
3. The first system disk automatically becomes the default disk. With multiple disks, the default boot disk is set by `PUT /workers/{worker_id}/default-disk` with `disk` (`os_tag`); the second and later disks never rewrite the default disk.

> **Note:** A master image must not be exported directly to a device for boot. The control plane also supports `empty` blank disks for manual installation; production uses master-image cloning, and a blank disk must still pass the same verification after installation.

## Verify

- After the bound device reboots, the boot serial console should show one successful authentication in the firmware phase and one in the kernel phase, followed by the rootfs mount and a `login:` prompt.
- No `0x7f22208e`, `ret=401`, keep-alive disconnects or emergency mode.
- A packet capture of the data plane shows only TLS on `4421`; the disk contains no identity or credentials, and cloned disks have distinct NQN, `os_tag` and credentials.

## What's next

- Full master-image workflow and scripts: [Customize a Master Image](./system-disk.md)
- Data-plane port and readiness conditions: [Data Plane and Readiness](../concepts/storage/data-plane.md)
- Credential operations after creation: [Operations](./operations.md)
- Full parameters and error codes for the create endpoint: [System Disks API](../reference/api/disks.md)
