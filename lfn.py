#!/usr/bin/env python3

import os
import sys

import llfuse
from argparse import ArgumentParser
import errno
import logging
import stat as stat_m
from llfuse import FUSEError
from os import fsencode, fsdecode
from collections import defaultdict

import faulthandler
faulthandler.enable()

log = logging.getLogger(__name__)

################### Database and name translation logic
import hashlib
from peewee import SqliteDatabase, Model, CharField

if "LFN_DB" in os.environ:
    dbPath = os.environ["LFN_DB"]
elif "XDG_DATA_HOME" in os.environ:
    dbPath = os.environ["XDG_DATA_HOME"] + "/LongFileName.db"
else:
    dbPath = os.path.expanduser("~/.local/share/LongFileName.db")

db = SqliteDatabase(dbPath, pragmas={
    'journal_mode': 'wal',
    'cache_size': -1024 * 64
})

class filenameTranslation(Model):
    digest = CharField(index=True)
    name = CharField()
    
    class Meta:
        database = db

print(f"Use database file '{dbPath}'")
db.connect()
db.create_tables([filenameTranslation])

def sha256sum(string):
    hasher = hashlib.sha256()
    hasher.update(string.encode())
    return hasher.hexdigest()
    
def virtual_to_real_path(path):
    real_path = []
    for chunk in path.split("/"):
        if len(chunk.encode()) > 255:
            digest = sha256sum(chunk)
            filenameTranslation.get_or_create(
                digest = digest,
                defaults = {
                    "name": chunk
                }
            )
            real_path.append(f".LFN.{digest}")
        else:
            real_path.append(chunk)
    real_path = "/".join(real_path)
    if path.endswith("/"):
        real_path += "/"
    return real_path

def real_to_virtual_path(path):
    virtual_path = []
    for chunk in path.split("/"):
        if chunk.startswith(".LFN.") and (record := filenameTranslation.get_or_none(digest=chunk[5:])):
            virtual_path.append(record.name)
        else:
            virtual_path.append(chunk)
    virtual_path = "/".join(virtual_path)
    if path.endswith("/"):
        virtual_path += "/"
    return virtual_path

################### Database and name translation logic

class Operations(llfuse.Operations):

    def __init__(self, source):
        super().__init__()
        self._inode_path_map = { llfuse.ROOT_INODE: source }
        self._lookup_cnt = defaultdict(lambda : 0)
        self._fd_inode_map = dict()
        self._inode_fd_map = dict()
        self._fd_open_count = dict()

    def _inode_to_path(self, inode):
        try:
            val = self._inode_path_map[inode]
        except KeyError:
            raise FUSEError(errno.ENOENT)

        if isinstance(val, set):
            # In case of hardlinks, pick any path
            val = next(iter(val))
        return val

    def _add_path(self, inode, path):
        log.debug('_add_path for %d, %s', inode, path)
        self._lookup_cnt[inode] += 1

        # With hardlinks, one inode may map to multiple paths.
        if inode not in self._inode_path_map:
            self._inode_path_map[inode] = path
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.add(path)
        elif val != path:
            self._inode_path_map[inode] = { path, val }

    def forget(self, inode_list):
        for (inode, nlookup) in inode_list:
            if self._lookup_cnt[inode] > nlookup:
                self._lookup_cnt[inode] -= nlookup
                continue
            log.debug('forgetting about inode %d', inode)
            assert inode not in self._inode_fd_map
            del self._lookup_cnt[inode]
            try:
                del self._inode_path_map[inode]
            except KeyError: # may have been deleted
                pass

    def lookup(self, inode_p, name, ctx=None):
        name = fsdecode(name)
        name = virtual_to_real_path(name)
        log.debug('lookup for %s in %d', name, inode_p)
        path = os.path.join(self._inode_to_path(inode_p), name)
        attr = self._getattr(path=path)
        if name != '.' and name != '..':
            self._add_path(attr.st_ino, path)
        return attr

    def getattr(self, inode, ctx=None):
        if inode in self._inode_fd_map:
            return self._getattr(fd=self._inode_fd_map[inode])
        else:
            return self._getattr(path=self._inode_to_path(inode))

    def _getattr(self, path=None, fd=None):
        assert fd is None or path is None
        assert not(fd is None and path is None)
        try:
            if fd is None:
                path = virtual_to_real_path(path)
                stat = os.lstat(path)
            else:
                stat = os.fstat(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

        entry = llfuse.EntryAttributes()
        for attr in ('st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                     'st_rdev', 'st_size', 'st_atime_ns', 'st_mtime_ns',
                     'st_ctime_ns'):
            setattr(entry, attr, getattr(stat, attr))
        entry.generation = 0
        entry.entry_timeout = 5
        entry.attr_timeout = 5
        entry.st_blksize = 512
        entry.st_blocks = ((entry.st_size+entry.st_blksize-1) // entry.st_blksize)

        return entry

    def readlink(self, inode, ctx):
        path = self._inode_to_path(inode)
        try:
            target = os.readlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        return fsencode(target)

    def opendir(self, inode, ctx):
        return inode

    def readdir(self, inode, off):
        path = self._inode_to_path(inode)
        log.debug('reading %s', path)
        entries = []
        for name in os.listdir(path):
            attr = self._getattr(path=os.path.join(path, name))
            entries.append((attr.st_ino, real_to_virtual_path(name), attr))

        log.debug('read %d entries, starting at %d', len(entries), off)

        # This is not fully posix compatible. If there are hardlinks
        # (two names with the same inode), we don't have a unique
        # offset to start in between them. Note that we cannot simply
        # count entries, because then we would skip over entries
        # (or return them more than once) if the number of directory
        # entries changes between two calls to readdir().
        for (ino, name, attr) in sorted(entries):
            if ino <= off:
                continue
            yield (fsencode(name), attr, ino)

    def unlink(self, inode_p, name, ctx):
        name = fsdecode(name)
        name = virtual_to_real_path(name)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.unlink(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            self._forget_path(inode, path)

    def rmdir(self, inode_p, name, ctx):
        name = fsdecode(name)
        name = virtual_to_real_path(name)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            inode = os.lstat(path).st_ino
            os.rmdir(path)
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode in self._lookup_cnt:
            self._forget_path(inode, path)

    def _forget_path(self, inode, path):
        log.debug('forget %s for %d', path, inode)
        val = self._inode_path_map[inode]
        if isinstance(val, set):
            val.remove(path)
            if len(val) == 1:
                self._inode_path_map[inode] = next(iter(val))
        else:
            del self._inode_path_map[inode]

    def symlink(self, inode_p, name, target, ctx):
        name = fsdecode(name)
        target = fsdecode(target)
        parent = self._inode_to_path(inode_p)
        path = os.path.join(parent, name)
        try:
            os.symlink(target, path)
            os.chown(path, ctx.uid, ctx.gid, follow_symlinks=False)
        except OSError as exc:
            raise FUSEError(exc.errno)
        stat = os.lstat(path)
        self._add_path(stat.st_ino, path)
        return self.getattr(stat.st_ino)

    def rename(self, inode_p_old, name_old, inode_p_new, name_new, ctx):
        name_old = fsdecode(name_old)
        name_new = fsdecode(name_new)
        parent_old = self._inode_to_path(inode_p_old)
        parent_new = self._inode_to_path(inode_p_new)
        path_old = os.path.join(parent_old, name_old)
        path_old = virtual_to_real_path(path_old)
        path_new = os.path.join(parent_new, name_new)
        path_new = virtual_to_real_path(path_new)
        try:
            os.rename(path_old, path_new)
            inode = os.lstat(path_new).st_ino
        except OSError as exc:
            raise FUSEError(exc.errno)
        if inode not in self._lookup_cnt:
            return

        val = self._inode_path_map[inode]
        if isinstance(val, set):
            assert len(val) > 1
            val.add(path_new)
            val.remove(path_old)
        else:
            assert val == path_old
            self._inode_path_map[inode] = path_new

    def link(self, inode, new_inode_p, new_name, ctx):
        new_name = fsdecode(new_name)
        new_name = virtual_to_real_path(new_name)
        parent = self._inode_to_path(new_inode_p)
        path = os.path.join(parent, new_name)
        try:
            os.link(self._inode_to_path(inode), path, follow_symlinks=False)
        except OSError as exc:
            raise FUSEError(exc.errno)
        self._add_path(inode, path)
        return self.getattr(inode)

    def setattr(self, inode, attr, fields, fh, ctx):
        # We use the f* functions if possible so that we can handle
        # a setattr() call for an inode without associated directory
        # handle.
        if fh is None:
            path_or_fh = self._inode_to_path(inode)
            truncate = os.truncate
            chmod = os.chmod
            chown = os.chown
            stat = os.lstat
        else:
            path_or_fh = fh
            truncate = os.ftruncate
            chmod = os.fchmod
            chown = os.fchown
            stat = os.fstat

        try:
            if fields.update_size:
                truncate(path_or_fh, attr.st_size)

            if fields.update_mode:
                # Under Linux, chmod always resolves symlinks so we should
                # actually never get a setattr() request for a symbolic
                # link.
                assert not stat_m.S_ISLNK(attr.st_mode)
                chmod(path_or_fh, stat_m.S_IMODE(attr.st_mode))

            if fields.update_uid:
                chown(path_or_fh, attr.st_uid, -1, follow_symlinks=False)

            if fields.update_gid:
                chown(path_or_fh, -1, attr.st_gid, follow_symlinks=False)

            if fields.update_atime and fields.update_mtime:
                # utime accepts both paths and file descriptiors
                os.utime(path_or_fh, None, follow_symlinks=False,
                         ns=(attr.st_atime_ns, attr.st_mtime_ns))
            elif fields.update_atime or fields.update_mtime:
                # We can only set both values, so we first need to retrieve the
                # one that we shouldn't be changing.
                oldstat = stat(path_or_fh)
                if not fields.update_atime:
                    attr.st_atime_ns = oldstat.st_atime_ns
                else:
                    attr.st_mtime_ns = oldstat.st_mtime_ns
                os.utime(path_or_fh, None, follow_symlinks=False,
                         ns=(attr.st_atime_ns, attr.st_mtime_ns))

        except OSError as exc:
            raise FUSEError(exc.errno)

        return self.getattr(inode)

    def mknod(self, inode_p, name, mode, rdev, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        path = virtual_to_real_path(path)
        try:
            os.mknod(path, mode=(mode & ~ctx.umask), device=rdev)
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self._getattr(path=path)
        self._add_path(attr.st_ino, path)
        return attr

    def mkdir(self, inode_p, name, mode, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        path = virtual_to_real_path(path)
        try:
            os.mkdir(path, mode=(mode & ~ctx.umask))
            os.chown(path, ctx.uid, ctx.gid)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self._getattr(path=path)
        self._add_path(attr.st_ino, path)
        return attr

    def statfs(self, ctx):
        root = self._inode_path_map[llfuse.ROOT_INODE]
        stat_ = llfuse.StatvfsData()
        try:
            statfs = os.statvfs(root)
        except OSError as exc:
            raise FUSEError(exc.errno)
        for attr in ('f_bsize', 'f_frsize', 'f_blocks', 'f_bfree', 'f_bavail',
                     'f_files', 'f_ffree', 'f_favail'):
            setattr(stat_, attr, getattr(statfs, attr))
        stat_.f_namemax = statfs.f_namemax - (len(root)+1)
        return stat_

    def open(self, inode, flags, ctx):
        if inode in self._inode_fd_map:
            fd = self._inode_fd_map[inode]
            self._fd_open_count[fd] += 1
            return fd
        assert flags & os.O_CREAT == 0
        try:
            fd = os.open(self._inode_to_path(inode), flags)
        except OSError as exc:
            raise FUSEError(exc.errno)
        self._inode_fd_map[inode] = fd
        self._fd_inode_map[fd] = inode
        self._fd_open_count[fd] = 1
        return fd

    def create(self, inode_p, name, mode, flags, ctx):
        path = os.path.join(self._inode_to_path(inode_p), fsdecode(name))
        path = virtual_to_real_path(path)
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_TRUNC)
        except OSError as exc:
            raise FUSEError(exc.errno)
        attr = self._getattr(fd=fd)
        self._add_path(attr.st_ino, path)
        self._inode_fd_map[attr.st_ino] = fd
        self._fd_inode_map[fd] = attr.st_ino
        self._fd_open_count[fd] = 1
        return (fd, attr)

    def read(self, fd, offset, length):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, length)

    def write(self, fd, offset, buf):
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, buf)

    def release(self, fd):
        if self._fd_open_count[fd] > 1:
            self._fd_open_count[fd] -= 1
            return

        del self._fd_open_count[fd]
        inode = self._fd_inode_map[fd]
        del self._inode_fd_map[inode]
        del self._fd_inode_map[fd]
        try:
            os.close(fd)
        except OSError as exc:
            raise FUSEError(exc.errno)

def init_logging(debug=False):
    formatter = logging.Formatter('%(asctime)s.%(msecs)03d %(threadName)s: '
                                  '[%(name)s] %(message)s', datefmt="%Y-%m-%d %H:%M:%S")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    if debug:
        handler.setLevel(logging.DEBUG)
        root_logger.setLevel(logging.DEBUG)
    else:
        handler.setLevel(logging.INFO)
        root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def parse_args(args):
    '''Parse command line'''

    parser = ArgumentParser()

    parser.add_argument('source', type=str,
                        help='Directory tree to mirror')
    parser.add_argument('mountpoint', type=str,
                        help='Where to mount the file system')
    parser.add_argument('--debug', action='store_true', default=False,
                        help='Enable debugging output')
    parser.add_argument('--debug-fuse', action='store_true', default=False,
                        help='Enable FUSE debugging output')

    return parser.parse_args(args)


def main():
    options = parse_args(sys.argv[1:])
    if options.source == "/":
        # If you remove this check and set source as "/" it crashes anyway
        print("source can't be /")
        exit(1)
    init_logging(options.debug)
    operations = Operations(options.source)

    log.debug('Mounting...')
    fuse_options = set(llfuse.default_options)
    fuse_options.add('fsname=passthroughfs')
    if options.debug_fuse:
        fuse_options.add('debug')
    llfuse.init(operations, options.mountpoint, fuse_options)

    try:
        log.debug('Entering main loop..')
        # Run single threaded because sqlite's limitation
        llfuse.main(workers=1)
    except:
        llfuse.close(unmount=False)
        raise

    log.debug('Unmounting..')
    llfuse.close()

if __name__ == '__main__':
    main()
