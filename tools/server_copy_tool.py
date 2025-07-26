#!/usr/bin/env python3
"""
AYON Server-Side File Copy Tool
------------------------------
Optimized file copying tool that runs on the storage server to avoid
network bottlenecks. Supports Linux reflink/rsync and Windows robocopy.

Usage:
    python server_copy_tool.py copy_file <src> <dst> [--verify]
    python server_copy_tool.py copy_batch <batch_file.json>
    python server_copy_tool.py test_speed <src> <dst>

Features:
- Platform-optimized copy commands (Linux: cp --reflink, rsync; Windows: robocopy)
- Atomic operations (copy to temp, then rename)
- MD5 checksum verification
- Batch processing for multiple files
- Filesystem-aware optimizations
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import List, Dict, Tuple


def log_info(message: str):
    """Simple logging to stderr."""
    print(f"[INFO] {message}", file=sys.stderr)


def log_error(message: str):
    """Simple error logging to stderr."""
    print(f"[ERROR] {message}", file=sys.stderr)


def get_filesystem_type(path: str) -> str:
    """Detect filesystem type on Linux for optimization."""
    if platform.system() != "Linux" or not shutil.which("df"):
        return "unknown"
    
    try:
        # Get the directory to check the mount point
        check_path = os.path.dirname(path) if os.path.isfile(path) else path
        if not check_path:
            check_path = "."
            
        result = subprocess.run(
            ["df", "-T", check_path], 
            capture_output=True, text=True, check=True, timeout=10
        )
        
        # Parse df output - filesystem type is in second column
        lines = result.stdout.strip().split('\n')
        if len(lines) > 1:
            return lines[-1].split()[1]
    except (subprocess.CalledProcessError, IndexError, subprocess.TimeoutExpired) as e:
        log_error(f"Could not determine filesystem for {path}: {e}")
    
    return "unknown"


def get_optimal_copy_command(src: str, dst: str) -> List[str]:
    """Get the fastest copy command for the current platform and filesystem."""
    system = platform.system()
    
    if system == "Linux":
        fs_type = get_filesystem_type(src)
        try:
            file_size = os.path.getsize(src)
        except OSError:
            file_size = 0
        
        log_info(f"Source filesystem: {fs_type}, file size: {file_size} bytes")
        
        # For very large files (>1GB) on copy-on-write filesystems
        if file_size > 1024 * 1024 * 1024 and fs_type in ["btrfs", "xfs", "ext4"]:
            log_info("Using cp with reflink for large file on CoW filesystem")
            return ["cp", "--reflink=auto", "--sparse=always", "--preserve=all", src, dst]
        
        # For network filesystems, use rsync with resume capability
        elif fs_type in ["cifs", "nfs", "smb", "fuse.cifs"]:
            log_info("Using rsync for network filesystem")
            return ["rsync", "-av", "--inplace", "--no-whole-file", src, dst]
        
        # For large files on regular filesystems, use dd with progress
        elif file_size > 1024 * 1024 * 1024:  # >1GB
            log_info("Using dd for large file")
            return ["dd", f"if={src}", f"of={dst}", "bs=64M", "status=progress"]
        
        # Default optimized copy for Linux
        else:
            log_info("Using cp with reflink auto-detection")
            return ["cp", "--reflink=auto", "--preserve=all", src, dst]
            
    elif system == "Windows":
        # Use robocopy for maximum performance on Windows
        src_dir = os.path.dirname(src)
        dst_dir = os.path.dirname(dst)
        filename = os.path.basename(src)
        
        log_info("Using robocopy for Windows")
        return [
            "robocopy", src_dir, dst_dir, filename,
            "/MT:16",  # Use 16 threads for maximum speed
            "/R:1",    # Only 1 retry
            "/W:1",    # Wait 1 second between retries
            "/NP",     # No progress (we handle our own)
            "/NJH",    # No job header
            "/NJS",    # No job summary
            "/NDL"     # No directory listing
        ]
    
    # Fallback for macOS or other Unix systems
    log_info("Using standard cp command")
    return ["cp", "-p", src, dst]


def calculate_md5(filepath: str, chunk_size: int = 1024 * 1024) -> str:
    """Calculate MD5 checksum of a file efficiently."""
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            # Read in larger chunks for better performance
            for chunk in iter(lambda: f.read(chunk_size), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except IOError as e:
        log_error(f"Failed to calculate MD5 for {filepath}: {e}")
        return ""


def ensure_directory_exists(filepath: str):
    """Create directory structure for the given file path."""
    directory = os.path.dirname(filepath)
    if directory and not os.path.exists(directory):
        try:
            os.makedirs(directory, exist_ok=True)
            log_info(f"Created directory: {directory}")
        except OSError as e:
            raise OSError(f"Failed to create directory {directory}: {e}")


def copy_file_atomic(src: str, dst: str, verify: bool = True) -> Dict:
    """
    Safely copy a file with atomic operation and optional verification.
    
    Process:
    1. Copy to temporary file
    2. Verify checksum if requested
    3. Atomically rename to final destination
    """
    start_time = time.time()
    temp_dst = f"{dst}.{os.getpid()}.tmp"
    
    result = {
        "success": False,
        "source": src,
        "destination": dst,
        "error": None,
        "size": 0,
        "duration": 0,
        "verified": verify
    }
    
    try:
        # Validate source file exists
        if not os.path.exists(src):
            raise FileNotFoundError(f"Source file not found: {src}")
        
        # Get source file info
        src_stat = os.stat(src)
        result["size"] = src_stat.st_size
        log_info(f"Copying {src} ({result['size']} bytes) -> {dst}")
        
        # Create destination directory
        ensure_directory_exists(dst)
        
        # Calculate source hash for verification (before copy)
        src_hash = ""
        if verify:
            log_info("Calculating source file checksum...")
            src_hash = calculate_md5(src)
            if not src_hash:
                raise ValueError("Failed to calculate source file checksum")
        
        # Execute optimized copy command to temporary file
        cmd = get_optimal_copy_command(src, temp_dst)
        log_info(f"Executing: {' '.join(cmd)}")
        
        copy_result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=3600  # 1 hour timeout for very large files
        )
        
        # Handle platform-specific return codes
        is_success = copy_result.returncode == 0
        if platform.system() == "Windows":
            # Robocopy returns 0-7 for success, 8+ for errors
            is_success = copy_result.returncode < 8
        
        if not is_success:
            error_msg = f"Copy command failed (code {copy_result.returncode}): {copy_result.stderr}"
            raise subprocess.CalledProcessError(copy_result.returncode, cmd, error_msg)
        
        # Verify the copied file exists and has correct size
        if not os.path.exists(temp_dst):
            raise FileNotFoundError("Temporary destination file was not created")
        
        temp_stat = os.stat(temp_dst)
        if temp_stat.st_size != src_stat.st_size:
            raise ValueError(f"Size mismatch: source {src_stat.st_size} != destination {temp_stat.st_size}")
        
        # Verify checksum if requested
        if verify and src_hash:
            log_info("Verifying destination file checksum...")
            dst_hash = calculate_md5(temp_dst)
            if src_hash != dst_hash:
                raise ValueError(f"Checksum mismatch: source {src_hash} != destination {dst_hash}")
            log_info("Checksum verification passed")
        
        # Atomic rename to final destination
        log_info(f"Moving {temp_dst} -> {dst}")
        shutil.move(temp_dst, dst)
        
        # Final verification that destination exists
        if not os.path.exists(dst):
            raise FileNotFoundError("Final destination file was not created")
        
        result["success"] = True
        result["duration"] = time.time() - start_time
        log_info(f"Copy completed successfully in {result['duration']:.2f} seconds")
        
        return result
        
    except Exception as e:
        result["error"] = str(e)
        result["duration"] = time.time() - start_time
        log_error(f"Copy failed after {result['duration']:.2f} seconds: {e}")
        
        # Cleanup temporary file on failure
        if os.path.exists(temp_dst):
            try:
                os.remove(temp_dst)
                log_info(f"Cleaned up temporary file: {temp_dst}")
            except OSError as cleanup_err:
                log_error(f"Failed to cleanup {temp_dst}: {cleanup_err}")
        
        return result


def copy_files_batch(file_operations: List[Dict]) -> Dict:
    """Process multiple file copy operations."""
    start_time = time.time()
    results = []
    total_size = 0
    successful_count = 0
    
    log_info(f"Starting batch copy of {len(file_operations)} files")
    
    for i, operation in enumerate(file_operations, 1):
        src = operation["src"]
        dst = operation["dst"] 
        verify = operation.get("verify", True)
        
        log_info(f"Processing file {i}/{len(file_operations)}: {src}")
        
        result = copy_file_atomic(src, dst, verify)
        results.append(result)
        
        if result["success"]:
            successful_count += 1
            total_size += result["size"]
        else:
            # Stop on first failure for safety
            log_error(f"Batch copy stopped due to failure: {result['error']}")
            break
    
    duration = time.time() - start_time
    batch_result = {
        "success": successful_count == len(file_operations),
        "total_files": len(file_operations),
        "successful_files": successful_count,
        "total_size": total_size,
        "duration": duration,
        "results": results
    }
    
    if batch_result["success"]:
        log_info(f"Batch copy completed: {successful_count} files, {total_size} bytes in {duration:.2f}s")
    else:
        log_error(f"Batch copy failed: {successful_count}/{len(file_operations)} files completed")
    
    return batch_result


def test_copy_speed(src: str, dst: str) -> Dict:
    """Test copy speed and provide recommendations."""
    log_info(f"Testing copy speed: {src} -> {dst}")
    
    if not os.path.exists(src):
        return {"error": "Source file not found"}
    
    file_size = os.path.getsize(src)
    fs_type = get_filesystem_type(src)
    
    # Test different copy methods
    methods = []
    
    if platform.system() == "Linux":
        methods = [
            (["cp", src, dst + ".test1"], "Standard cp"),
            (["cp", "--reflink=auto", src, dst + ".test2"], "cp with reflink"),
            (["rsync", "-a", src, dst + ".test3"], "rsync")
        ]
    elif platform.system() == "Windows":
        src_dir = os.path.dirname(src)
        dst_dir = os.path.dirname(dst)
        filename = os.path.basename(src)
        methods = [
            (["copy", src, dst + ".test1"], "Windows copy"),
            (["robocopy", src_dir, dst_dir, filename + ".test2", "/MT:1"], "robocopy single-thread"),
            (["robocopy", src_dir, dst_dir, filename + ".test3", "/MT:8"], "robocopy multi-thread")
        ]
    
    results = {
        "file_size": file_size,
        "filesystem": fs_type,
        "methods": []
    }
    
    for cmd, name in methods:
        try:
            start = time.time()
            subprocess.run(cmd, check=True, capture_output=True)
            duration = time.time() - start
            speed_mbps = (file_size / (1024 * 1024)) / duration if duration > 0 else 0
            
            results["methods"].append({
                "name": name,
                "duration": duration,
                "speed_mbps": speed_mbps
            })
            
            # Cleanup test files
            for i in range(1, 4):
                test_file = dst + f".test{i}"
                if os.path.exists(test_file):
                    os.remove(test_file)
                    
        except subprocess.CalledProcessError:
            continue
    
    return results


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="AYON Server-Side File Copy Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Copy single file with verification
    python server_copy_tool.py copy_file /src/file.exr /dst/file.exr --verify
    
    # Copy multiple files from JSON batch file  
    python server_copy_tool.py copy_batch operations.json
    
    # Test copy speed for optimization
    python server_copy_tool.py test_speed /src/test.exr /dst/test.exr
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Single file copy command
    copy_parser = subparsers.add_parser(
        "copy_file", 
        help="Copy a single file with atomic operation"
    )
    copy_parser.add_argument("src", help="Source file path")
    copy_parser.add_argument("dst", help="Destination file path")
    copy_parser.add_argument(
        "--verify", 
        action="store_true", 
        help="Verify file integrity with MD5 checksum"
    )
    copy_parser.add_argument(
        "--no-verify", 
        action="store_true", 
        help="Skip checksum verification for speed"
    )
    
    # Batch copy command
    batch_parser = subparsers.add_parser(
        "copy_batch", 
        help="Copy multiple files from JSON batch file"
    )
    batch_parser.add_argument(
        "batch_file", 
        help="JSON file containing array of {src, dst, verify} objects"
    )
    
    # Speed test command
    test_parser = subparsers.add_parser(
        "test_speed", 
        help="Test copy speed and provide optimization recommendations"
    )
    test_parser.add_argument("src", help="Source file for speed test")
    test_parser.add_argument("dst", help="Destination path for speed test")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Execute commands
    try:
        if args.command == "copy_file":
            verify = args.verify and not args.no_verify
            result = copy_file_atomic(args.src, args.dst, verify)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result["success"] else 1)
            
        elif args.command == "copy_batch":
            if not os.path.exists(args.batch_file):
                log_error(f"Batch file not found: {args.batch_file}")
                sys.exit(1)
                
            with open(args.batch_file, 'r') as f:
                batch_data = json.load(f)
            
            if not isinstance(batch_data, list):
                log_error("Batch file must contain an array of copy operations")
                sys.exit(1)
            
            result = copy_files_batch(batch_data)
            print(json.dumps(result, indent=2))
            sys.exit(0 if result["success"] else 1)
            
        elif args.command == "test_speed":
            result = test_copy_speed(args.src, args.dst)
            print(json.dumps(result, indent=2))
            sys.exit(0)
            
    except KeyboardInterrupt:
        log_error("Operation cancelled by user")
        sys.exit(130)
    except Exception as e:
        log_error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 