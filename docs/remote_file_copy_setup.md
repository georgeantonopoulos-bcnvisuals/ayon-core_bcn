# AYON Remote File Copy Setup Guide

## Overview

The AYON Remote File Copy feature enables server-side file copying to dramatically improve performance when working with network storage. Instead of downloading files to client workstations and then uploading them to their final destination, files are copied directly on the storage server.

## Performance Benefits

- **Before**: 30 MB/s (Client ↔ Network Drive ↔ Client)
- **After**: 500-2000 MB/s+ (Direct server-side copy)
- **Speedup**: 15-60x faster for large files

## Architecture

```
[Client Workstation] --SSH--> [Storage Server] --Direct Copy--> [Same Storage]
```

The client sends copy commands via SSH to the storage server, which performs optimized platform-specific copy operations.

## Prerequisites

1. SSH access to your storage server
2. Python 3.6+ on the storage server
3. Network storage mounted on both client and server
4. AYON Core 1.1.7+ with remote copy support

## Setup Instructions

### 1. Deploy Server-Side Script

Copy `tools/server_copy_tool.py` to your storage server:

```bash
# On your storage server
sudo mkdir -p /opt/ayon
sudo cp server_copy_tool.py /opt/ayon/
sudo chmod +x /opt/ayon/server_copy_tool.py

# Test the script
python3 /opt/ayon/server_copy_tool.py --help
```

### 2. Configure SSH Access

Set up SSH key authentication for AYON workstations:

```bash
# On AYON workstation
ssh-keygen -t rsa -b 4096 -f ~/.ssh/ayon_storage_key
ssh-copy-id -i ~/.ssh/ayon_storage_key.pub user@storage-server

# Test SSH access
ssh -i ~/.ssh/ayon_storage_key user@storage-server "echo 'SSH OK'"
```

### 3. Configure AYON Settings

In AYON Studio Settings → Core → Remote File Copy:

```
✅ Enable Remote File Copy: True
🖥️ Storage Server SSH Host: storage-server.example.com
👤 SSH Username: ayon-user
🔑 SSH Private Key Path: /home/user/.ssh/ayon_storage_key
📁 Server Copy Script Path: /opt/ayon/server_copy_tool.py
⏱️ Copy Timeout (seconds): 3600
📏 Minimum File Size (MB): 100
✅ Test Connection on Startup: True
✅ Verify File Integrity: True
```

### 4. Test the Setup

```python
# Test from Python
from ayon_core.lib.remote_copy import get_remote_copier_from_settings

copier = get_remote_copier_from_settings("your_project_name")
if copier:
    result = copier.test_connection()
    print(f"Remote copy available: {result}")
```

## Usage Examples

### Automatic Mode (Recommended)

The FileTransaction system automatically chooses the best copy method:

```python
from ayon_core.lib.file_transaction import FileTransaction

# FileTransaction automatically uses remote copy for large files
transaction = FileTransaction(project_name="my_project")
transaction.add_with_auto_mode("/source/large_file.exr", "/dest/large_file.exr", prefer_remote=True)
transaction.process()
transaction.finalize()
```

### Manual Mode

Force remote copy for specific files:

```python
transaction = FileTransaction(project_name="my_project")
transaction.add("/source/file.exr", "/dest/file.exr", mode=FileTransaction.MODE_REMOTE)
transaction.process()
transaction.finalize()
```

### Publish Plugin Integration

```python
class MyPublishPlugin(pyblish.api.InstancePlugin):
    def process(self, instance):
        # Mark large files for remote transfer optimization
        instance.data["remote_transfers"] = [
            ("/work/render_001.exr", "/publish/render_001.exr"),
            ("/work/render_002.exr", "/publish/render_002.exr"),
        ]
        
        # The integration plugin will automatically handle these optimally
```

## Platform Optimizations

### Linux
- **CoW Filesystems** (btrfs, XFS, ext4): Uses `cp --reflink` for instant copies
- **Network Storage** (CIFS, NFS): Uses `rsync` with resume capability  
- **Large Files** (>1GB): Uses `dd` with progress reporting

### Windows
- **All Files**: Uses `robocopy` with multi-threading for maximum performance

## Troubleshooting

### Connection Issues

```bash
# Test SSH connectivity
ssh -i ~/.ssh/ayon_storage_key user@storage-server "python3 /opt/ayon/server_copy_tool.py --help"

# Check AYON logs
grep "Remote copy" /path/to/ayon/logs/ayon.log
```

### Performance Testing

```bash
# Test copy speed on storage server
python3 /opt/ayon/server_copy_tool.py test_speed /source/test.exr /dest/test.exr
```

### Fallback Behavior

Remote copy automatically falls back to local copy if:
- SSH connection fails
- Remote script is not available
- Server-side copy fails
- File size is below threshold

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable/disable remote copy |
| `ssh_host` | `""` | Storage server hostname/IP |
| `ssh_user` | `""` | SSH username |
| `ssh_key_path` | `""` | Path to SSH private key |
| `script_path` | `/opt/ayon/server_copy_tool.py` | Server script path |
| `timeout` | `3600` | Copy timeout in seconds |
| `min_file_size_mb` | `100` | Minimum file size for remote copy |
| `test_connection_on_init` | `true` | Test SSH on startup |
| `verify_checksums` | `true` | Verify file integrity |

## Security Considerations

1. **SSH Keys**: Use dedicated SSH keys with restricted permissions
2. **User Access**: Create dedicated user account for AYON operations
3. **Script Location**: Place script in protected directory (`/opt/ayon/`)
4. **Network**: Ensure SSH connections are on trusted networks

## Best Practices

1. **File Size Threshold**: Set `min_file_size_mb` to 50-100MB for optimal performance
2. **SSH Configuration**: Use SSH connection multiplexing for better performance
3. **Monitoring**: Monitor copy performance and adjust settings accordingly
4. **Testing**: Always test with non-critical data first

## Monitoring & Metrics

Check AYON logs for performance metrics:

```
[INFO] Remote batch copy completed: 5 files, 2147483648 bytes in 12.34s
[INFO] Remote copy: fallback to local copy (file size below threshold)
[INFO] Remote copy optimization: 15.2x speedup (2.3s vs 35.1s local)
```

## Limitations

- Requires SSH access to storage server
- Both source and destination must be accessible from storage server
- Network latency affects SSH command execution
- Platform-specific optimizations may vary

For additional support, check the AYON Core documentation or contact your system administrator. 