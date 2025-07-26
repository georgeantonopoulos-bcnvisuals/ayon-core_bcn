"""
Remote File Copy Handler for AYON
---------------------------------
Client-side module for executing server-side file copy operations via SSH.
Integrates with AYON's existing FileTransaction system for optimized copying.

Features:
- SSH-based remote execution of optimized copy commands
- Batch processing for multiple files  
- Fallback to local copy if remote fails
- Integration with AYON settings system
"""
import json
import os
import tempfile
import time
from typing import List, Dict, Optional, Tuple

from ayon_core.lib.execute import run_subprocess
from ayon_core.lib.log import Logger

log = Logger.get_logger(__name__)


class RemoteFileCopier:
    """Executes server-side file operations via SSH."""
    
    def __init__(self, ssh_host: str, ssh_user: str, script_path: str, 
                 ssh_key_path: Optional[str] = None, timeout: int = 3600):
        """
        Initialize remote file copier.
        
        Args:
            ssh_host: Hostname or IP of the storage server
            ssh_user: SSH username for authentication
            script_path: Path to server_copy_tool.py on the remote server
            ssh_key_path: Optional path to SSH private key file
            timeout: Timeout in seconds for copy operations
        """
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.script_path = script_path
        self.ssh_key_path = ssh_key_path
        self.timeout = timeout
        self._connection_tested = False
        
        log.info(f"Remote copier configured: {ssh_user}@{ssh_host}:{script_path}")
    
    def _get_ssh_base_command(self) -> List[str]:
        """Build base SSH command with authentication."""
        cmd = ["ssh"]
        
        # Add SSH options for better reliability
        cmd.extend([
            "-o", "BatchMode=yes",  # Non-interactive
            "-o", "ConnectTimeout=30",  # Connection timeout
            "-o", "ServerAliveInterval=60",  # Keep alive
            "-o", "ServerAliveCountMax=3",  # Max keep alive attempts
            "-o", "StrictHostKeyChecking=no",  # Accept unknown hosts (for AWS environments)
        ])
        
        # Add SSH key if specified
        if self.ssh_key_path and os.path.exists(self.ssh_key_path):
            cmd.extend(["-i", self.ssh_key_path])
        
        cmd.append(f"{self.ssh_user}@{self.ssh_host}")
        return cmd
    
    def test_connection(self) -> bool:
        """Test SSH connection and verify script exists."""
        try:
            log.info("Testing SSH connection to storage server...")
            
            # Test basic SSH connection
            cmd = self._get_ssh_base_command()
            cmd.extend(["echo", "connection_test_ok"])
            
            result = run_subprocess(cmd, timeout=30)
            if "connection_test_ok" not in result:
                log.error("SSH connection test failed")
                return False
            
            # Test that copy script exists and is executable
            cmd = self._get_ssh_base_command()
            cmd.extend(["python3", self.script_path, "--help"])
            
            result = run_subprocess(cmd, timeout=30)
            if "AYON Server-Side File Copy Tool" not in result:
                log.error(f"Copy script not found or not working at {self.script_path}")
                return False
            
            log.info("SSH connection and copy script verified successfully")
            self._connection_tested = True
            return True
            
        except Exception as e:
            log.error(f"SSH connection test failed: {e}")
            return False
    
    def copy_file(self, src: str, dst: str, verify: bool = True) -> Dict:
        """
        Copy a single file via remote execution.
        
        Args:
            src: Source file path (must be accessible from storage server)
            dst: Destination file path  
            verify: Whether to verify file integrity with checksum
            
        Returns:
            Dict with success status and operation details
        """
        if not self._connection_tested and not self.test_connection():
            return {"success": False, "error": "SSH connection not available"}
        
        try:
            log.info(f"Remote copy: {src} -> {dst}")
            
            # Build SSH command to execute copy script
            cmd = self._get_ssh_base_command()
            cmd.extend([
                "python3", self.script_path, "copy_file", src, dst
            ])
            
            if verify:
                cmd.append("--verify")
            else:
                cmd.append("--no-verify")
            
            # Execute remote copy
            start_time = time.time()
            output = run_subprocess(cmd, timeout=self.timeout)
            duration = time.time() - start_time
            
            # Parse JSON result from script
            result = json.loads(output)
            result["remote_duration"] = duration
            
            if result["success"]:
                log.info(f"Remote copy completed successfully in {duration:.2f}s")
            else:
                log.error(f"Remote copy failed: {result.get('error')}")
            
            return result
            
        except Exception as e:
            log.error(f"Remote copy execution failed: {e}")
            return {"success": False, "error": str(e)}
    
    def copy_files_batch(self, file_pairs: List[Tuple[str, str]], 
                        verify: bool = True) -> Dict:
        """
        Copy multiple files via remote batch execution.
        
        Args:
            file_pairs: List of (source, destination) tuples
            verify: Whether to verify file integrity with checksums
            
        Returns:
            Dict with batch operation results
        """
        if not self._connection_tested and not self.test_connection():
            return {"success": False, "error": "SSH connection not available"}
        
        if not file_pairs:
            return {"success": True, "total_files": 0, "results": []}
        
        try:
            log.info(f"Remote batch copy: {len(file_pairs)} files")
            
            # Create batch file data
            batch_data = [
                {"src": src, "dst": dst, "verify": verify}
                for src, dst in file_pairs
            ]
            
            # Create temporary batch file locally
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.json', delete=False
            ) as f:
                json.dump(batch_data, f, indent=2)
                local_batch_file = f.name
            
            try:
                # Generate remote batch file path
                remote_batch_file = f"/tmp/ayon_batch_{os.getpid()}_{int(time.time())}.json"
                
                # Copy batch file to remote server
                log.debug(f"Uploading batch file to {remote_batch_file}")
                scp_cmd = ["scp"]
                if self.ssh_key_path and os.path.exists(self.ssh_key_path):
                    scp_cmd.extend(["-i", self.ssh_key_path])
                scp_cmd.extend([
                    "-o", "StrictHostKeyChecking=no",
                    local_batch_file,
                    f"{self.ssh_user}@{self.ssh_host}:{remote_batch_file}"
                ])
                
                run_subprocess(scp_cmd, timeout=60)
                
                # Execute remote batch copy
                cmd = self._get_ssh_base_command()
                cmd.extend([
                    "python3", self.script_path, "copy_batch", remote_batch_file
                ])
                
                start_time = time.time()
                output = run_subprocess(cmd, timeout=self.timeout)
                duration = time.time() - start_time
                
                # Parse batch results
                result = json.loads(output)
                result["remote_duration"] = duration
                
                # Cleanup remote batch file
                try:
                    cleanup_cmd = self._get_ssh_base_command()
                    cleanup_cmd.extend(["rm", "-f", remote_batch_file])
                    run_subprocess(cleanup_cmd, timeout=30)
                except Exception as cleanup_err:
                    log.warning(f"Failed to cleanup remote batch file: {cleanup_err}")
                
                if result["success"]:
                    log.info(
                        f"Remote batch copy completed: {result['successful_files']}/"
                        f"{result['total_files']} files in {duration:.2f}s"
                    )
                else:
                    log.error(
                        f"Remote batch copy failed: {result['successful_files']}/"
                        f"{result['total_files']} files completed"
                    )
                
                return result
                
            finally:
                # Cleanup local batch file
                try:
                    os.unlink(local_batch_file)
                except OSError:
                    pass
                    
        except Exception as e:
            log.error(f"Remote batch copy execution failed: {e}")
            return {
                "success": False, 
                "error": str(e),
                "total_files": len(file_pairs),
                "successful_files": 0
            }
    
    def get_filesystem_info(self, path: str) -> Dict:
        """Get filesystem information for a path on the remote server."""
        if not self._connection_tested and not self.test_connection():
            return {"error": "SSH connection not available"}
        
        try:
            # Use df to get filesystem information
            cmd = self._get_ssh_base_command()
            cmd.extend(["df", "-T", path])
            
            output = run_subprocess(cmd, timeout=30)
            lines = output.strip().split('\n')
            
            if len(lines) > 1:
                fields = lines[-1].split()
                return {
                    "filesystem": fields[1] if len(fields) > 1 else "unknown",
                    "mount_point": fields[6] if len(fields) > 6 else "unknown",
                    "available_space": fields[4] if len(fields) > 4 else "unknown"
                }
            
            return {"error": "Could not parse filesystem information"}
            
        except Exception as e:
            log.error(f"Failed to get filesystem info: {e}")
            return {"error": str(e)}


def get_remote_copier_from_settings(project_name: str) -> Optional[RemoteFileCopier]:
    """
    Create RemoteFileCopier from AYON project settings.
    
    Args:
        project_name: Name of the AYON project
        
    Returns:
        RemoteFileCopier instance if configured, None otherwise
    """
    try:
        from ayon_core.settings import get_project_settings
        
        settings = get_project_settings(project_name)
        remote_settings = settings.get("core", {}).get("remote_file_copy", {})
        
        if not remote_settings.get("enabled", False):
            log.debug("Remote file copy not enabled in project settings")
            return None
        
        required_fields = ["ssh_host", "ssh_user", "script_path"]
        missing_fields = [
            field for field in required_fields 
            if not remote_settings.get(field)
        ]
        
        if missing_fields:
            log.warning(f"Remote copy settings missing required fields: {missing_fields}")
            return None
        
        copier = RemoteFileCopier(
            ssh_host=remote_settings["ssh_host"],
            ssh_user=remote_settings["ssh_user"],
            script_path=remote_settings["script_path"],
            ssh_key_path=remote_settings.get("ssh_key_path"),
            timeout=remote_settings.get("timeout", 3600)
        )
        
        # Test connection during initialization if enabled
        if remote_settings.get("test_connection_on_init", True):
            if not copier.test_connection():
                log.warning("Remote copy connection test failed, disabling remote copy")
                return None
        
        return copier
        
    except Exception as e:
        log.error(f"Failed to create remote copier from settings: {e}")
        return None


def should_use_remote_copy(src: str, dst: str, file_size: int = 0, 
                          min_file_size: int = 100 * 1024 * 1024) -> bool:
    """
    Determine if remote copy should be used based on file characteristics.
    
    Args:
        src: Source file path
        dst: Destination file path  
        file_size: File size in bytes (0 to auto-detect)
        min_file_size: Minimum file size to use remote copy
        
    Returns:
        True if remote copy is recommended
    """
    try:
        # Auto-detect file size if not provided
        if file_size == 0 and os.path.exists(src):
            file_size = os.path.getsize(src)
        
        # Use remote copy for large files
        if file_size >= min_file_size:
            log.debug(f"Large file ({file_size} bytes) - recommending remote copy")
            return True
        
        # Check if paths suggest network storage (basic heuristics)
        network_indicators = ["/mnt/", "/cifs/", "//", "smb://", "nfs://"]
        src_is_network = any(indicator in src.lower() for indicator in network_indicators)
        dst_is_network = any(indicator in dst.lower() for indicator in network_indicators)
        
        if src_is_network and dst_is_network:
            log.debug("Both paths appear to be on network storage - recommending remote copy")
            return True
        
        return False
        
    except Exception as e:
        log.debug(f"Error determining remote copy recommendation: {e}")
        return False 