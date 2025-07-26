import os
import logging
import sys
import errno
from typing import Optional

from ayon_core.lib import create_hard_link

# this is needed until speedcopy for linux is fixed
if sys.platform == "win32":
    from speedcopy import copyfile
else:
    from shutil import copyfile


class DuplicateDestinationError(ValueError):
    """Error raised when transfer destination already exists in queue.

    The error is only raised if `allow_queue_replacements` is False on the
    FileTransaction instance and the added file to transfer is of a different
    src file than the one already detected in the queue.

    """


class FileTransaction:
    """File transaction with rollback options.

    The file transaction is a three-step process.

    1) Rename any existing files to a "temporary backup" during `process()`
    2) Copy the files to final destination during `process()`
    3) Remove any backed up files (*no rollback possible!) during `finalize()`

    Step 3 is done during `finalize()`. If not called the .bak files will
    remain on disk.

    These steps try to ensure that we don't overwrite half of any existing
    files e.g. if they are currently in use.

    Note:
        A regular filesystem is *not* a transactional file system and even
        though this implementation tries to produce a 'safe copy' with a
        potential rollback do keep in mind that it's inherently unsafe due
        to how filesystem works and a myriad of things could happen during
        the transaction that break the logic. A file storage could go down,
        permissions could be changed, other machines could be moving or writing
        files. A lot can happen.

    Warning:
        Any folders created during the transfer will not be removed.
    """

    MODE_COPY = 0
    MODE_HARDLINK = 1
    MODE_REMOTE = 2  # New mode for server-side copying

    def __init__(self, log=None, allow_queue_replacements=False, 
                 project_name: Optional[str] = None):
        if log is None:
            log = logging.getLogger("FileTransaction")

        self.log = log

        # The transfer queue
        # todo: make this an actual FIFO queue?
        self._transfers = {}

        # Destination file paths that a file was transferred to
        self._transferred = []

        # Backup file location mapping to original locations
        self._backup_to_original = {}

        self._allow_queue_replacements = allow_queue_replacements
        
        # Remote copy support
        self._project_name = project_name
        self._remote_copier = None
        self._remote_copy_enabled = False
        
        # Initialize remote copier if project is specified
        if project_name:
            self._init_remote_copier()

    def add(self, src, dst, mode=MODE_COPY):
        """Add a new file to transfer queue.

        Args:
            src (str): Source path.
            dst (str): Destination path.
            mode (MODE_COPY, MODE_HARDLINK, MODE_REMOTE): Transfer mode.
                MODE_COPY: Regular file copy
                MODE_HARDLINK: Create hardlink (same filesystem only)
                MODE_REMOTE: Server-side copy via SSH (requires configuration)
        """

        opts = {"mode": mode}

        src = os.path.normpath(os.path.abspath(src))
        dst = os.path.normpath(os.path.abspath(dst))

        if dst in self._transfers:
            queued_src = self._transfers[dst][0]
            if src == queued_src:
                self.log.debug(
                    "File transfer was already in queue: {} -> {}".format(
                        src, dst))
                return
            else:
                if not self._allow_queue_replacements:
                    raise DuplicateDestinationError(
                        "Transfer to destination is already in queue: "
                        "{} -> {}. It's not allowed to be replaced by "
                        "a new transfer from {}".format(
                            queued_src, dst, src
                        ))

                self.log.warning("File transfer in queue replaced..")
                self.log.debug(
                    "Removed from queue: {} -> {} replaced by {} -> {}".format(
                        queued_src, dst, src, dst))

        self._transfers[dst] = (src, opts)

    def _init_remote_copier(self):
        """Initialize remote copier from project settings."""
        try:
            from .remote_copy import get_remote_copier_from_settings
            
            self._remote_copier = get_remote_copier_from_settings(self._project_name)
            self._remote_copy_enabled = self._remote_copier is not None
            
            if self._remote_copy_enabled:
                self.log.info("Remote file copy enabled for project")
            else:
                self.log.debug("Remote file copy not configured or available")
                
        except ImportError:
            self.log.debug("Remote copy module not available")
        except Exception as e:
            self.log.warning(f"Failed to initialize remote copier: {e}")

    def add_with_auto_mode(self, src, dst, prefer_remote=False):
        """
        Add file to transfer queue with automatic mode selection.
        
        Args:
            src (str): Source path
            dst (str): Destination path  
            prefer_remote (bool): Prefer remote copy if available
        """
        # Determine optimal copy mode
        mode = self._determine_copy_mode(src, dst, prefer_remote)
        self.add(src, dst, mode)

    def _determine_copy_mode(self, src, dst, prefer_remote=False):
        """Determine the best copy mode for the given paths."""
        # Check if remote copy is available and should be used
        if self._remote_copy_enabled and prefer_remote:
            try:
                from .remote_copy import should_use_remote_copy
                
                if should_use_remote_copy(src, dst):
                    self.log.debug(f"Selected remote copy mode for {src} -> {dst}")
                    return self.MODE_REMOTE
            except Exception as e:
                self.log.debug(f"Remote copy check failed: {e}")
        
        # Check if hardlink is possible (same filesystem)
        try:
            src_stat = os.stat(src)
            dst_dir = os.path.dirname(dst)
            if os.path.exists(dst_dir):
                dst_stat = os.stat(dst_dir)
                if src_stat.st_dev == dst_stat.st_dev:
                    self.log.debug(f"Selected hardlink mode for {src} -> {dst}")
                    return self.MODE_HARDLINK
        except OSError:
            pass
        
        # Default to regular copy
        self.log.debug(f"Selected copy mode for {src} -> {dst}")
        return self.MODE_COPY

    def process(self):
        # Backup any existing files
        for dst, (src, _) in self._transfers.items():
            self.log.debug("Checking file ... {} -> {}".format(src, dst))
            path_same = self._same_paths(src, dst)
            if path_same or not os.path.exists(dst):
                continue

            # Backup original file
            # todo: add timestamp or uuid to ensure unique
            backup = dst + ".bak"
            self._backup_to_original[backup] = dst
            self.log.debug(
                "Backup existing file: {} -> {}".format(dst, backup))
            os.rename(dst, backup)

        # Group transfers by mode for efficient batch processing
        remote_transfers = []
        local_transfers = []
        
        for dst, (src, opts) in self._transfers.items():
            path_same = self._same_paths(src, dst)
            if path_same:
                self.log.debug(
                    "Source and destination are same files {} -> {}".format(
                        src, dst))
                continue

            if opts["mode"] == self.MODE_REMOTE:
                remote_transfers.append((src, dst))
            else:
                local_transfers.append((dst, src, opts))

        # Process remote transfers in batch for efficiency
        if remote_transfers and self._remote_copy_enabled:
            self._process_remote_transfers(remote_transfers)
        
        # Process local transfers individually
        for dst, src, opts in local_transfers:
            self._create_folder_for_file(dst)

            if opts["mode"] == self.MODE_COPY:
                self.log.debug("Copying file ... {} -> {}".format(src, dst))
                copyfile(src, dst)
            elif opts["mode"] == self.MODE_HARDLINK:
                self.log.debug("Hardlinking file ... {} -> {}".format(
                    src, dst))
                create_hard_link(src, dst)

            self._transferred.append(dst)

    def _process_remote_transfers(self, transfers):
        """Process remote transfers in batch."""
        if not self._remote_copier:
            self.log.warning("Remote copier not available, falling back to local copy")
            # Fallback to local copy
            for src, dst in transfers:
                self._create_folder_for_file(dst)
                self.log.debug("Copying file (fallback) ... {} -> {}".format(src, dst))
                copyfile(src, dst)
                self._transferred.append(dst)
            return

        try:
            self.log.info(f"Processing {len(transfers)} remote file transfers")
            
            # Execute batch remote copy
            result = self._remote_copier.copy_files_batch(transfers, verify=True)
            
            if result["success"]:
                # Mark all transfers as completed
                for src, dst in transfers:
                    self._transferred.append(dst)
                
                self.log.info(
                    f"Remote batch copy completed successfully: "
                    f"{result['successful_files']} files, "
                    f"{result.get('total_size', 0)} bytes in "
                    f"{result.get('duration', 0):.2f}s"
                )
            else:
                self.log.error(f"Remote batch copy failed: {result.get('error')}")
                
                # Fallback to local copy for failed transfers
                failed_transfers = []
                for i, (src, dst) in enumerate(transfers):
                    if i < len(result.get("results", [])):
                        transfer_result = result["results"][i]
                        if transfer_result["success"]:
                            self._transferred.append(dst)
                        else:
                            failed_transfers.append((src, dst))
                    else:
                        failed_transfers.append((src, dst))
                
                # Retry failed transfers locally
                if failed_transfers:
                    self.log.warning(f"Retrying {len(failed_transfers)} failed remote transfers locally")
                    for src, dst in failed_transfers:
                        try:
                            self._create_folder_for_file(dst)
                            self.log.debug("Copying file (fallback) ... {} -> {}".format(src, dst))
                            copyfile(src, dst)
                            self._transferred.append(dst)
                        except Exception as e:
                            self.log.error(f"Fallback copy failed for {src} -> {dst}: {e}")
                            raise
                            
        except Exception as e:
            self.log.error(f"Remote transfer processing failed: {e}")
            # Fallback to local copy for all transfers
            self.log.warning("Falling back to local copy for all remote transfers")
            for src, dst in transfers:
                try:
                    self._create_folder_for_file(dst)
                    self.log.debug("Copying file (fallback) ... {} -> {}".format(src, dst))
                    copyfile(src, dst)
                    self._transferred.append(dst)
                except Exception as fallback_err:
                    self.log.error(f"Fallback copy failed for {src} -> {dst}: {fallback_err}")
                    raise

    def finalize(self):
        # Delete any backed up files
        for backup in self._backup_to_original.keys():
            try:
                os.remove(backup)
            except OSError:
                self.log.error(
                    "Failed to remove backup file: {}".format(backup),
                    exc_info=True)

    def rollback(self):
        errors = 0
        last_exc = None
        # Rollback any transferred files
        for path in self._transferred:
            try:
                os.remove(path)
            except OSError as exc:
                last_exc = exc
                errors += 1
                self.log.error(
                    "Failed to rollback created file: {}".format(path),
                    exc_info=True)

        # Rollback the backups
        for backup, original in self._backup_to_original.items():
            try:
                os.rename(backup, original)
            except OSError as exc:
                last_exc = exc
                errors += 1
                self.log.error(
                    "Failed to restore original file: {} -> {}".format(
                        backup, original),
                    exc_info=True)

        if errors:
            self.log.error(
                "{} errors occurred during rollback.".format(errors),
                exc_info=True)
            raise last_exc

    @property
    def transferred(self):
        """Return the processed transfers destination paths"""
        return list(self._transferred)

    @property
    def backups(self):
        """Return the backup file paths"""
        return list(self._backup_to_original.keys())

    def _create_folder_for_file(self, path):
        dirname = os.path.dirname(path)
        try:
            os.makedirs(dirname)
        except OSError as e:
            if e.errno != errno.EEXIST:
                self.log.critical("An unexpected error occurred.")
                raise e

    def _same_paths(self, src, dst):
        # handles same paths but with C:/project vs c:/project
        if os.path.exists(src) and os.path.exists(dst):
            return os.stat(src) == os.stat(dst)

        return src == dst
