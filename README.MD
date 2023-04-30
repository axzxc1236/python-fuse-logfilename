# python-fuse-logfilename

A fuse filesystem which allows up to 1024 bytes long filename, I don't know how long the pathname can be, dolphin gives up around 4000 for me but in command line I got to over 6000.

Does the same thing as [i-rinat/insecure](https://github.com/i-rinat/insecure) and [i-rinat/longnamefs](https://github.com/i-rinat/longnamefs) but written in Python.

## How does this work?
Lets say you want to save 300 bytes long filename (which is a little bit easier to reach if you use CJK languages) "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五 六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"

it first create sha256 hash of the above long filename 06af4358ef9a02b3d30eb667c642a3529098aa4789c41fe745f595b6503f5a3f

Then it stores this mapping to database, then creates ".LFN.06af4358ef9a02b3d30eb667c642a3529098aa4789c41fe745f595b6503f5a3f" on your actual disk space.

When you browse through virtual disk with names starts with ".LFN." it tries to get the long filename in the background, and silently translates filename back to long filename

## Usage

required 3rd Python libraries: peewee llfuse

```
usage: lfn.py [-h] [--debug] [--debug-fuse] source mountpoint

positional arguments:
  source        Directory tree to mirror
  mountpoint    Where to mount the file system

options:
  -h, --help    show this help message and exit
  --debug       Enable debugging output
  --debug-fuse  Enable FUSE debugging output
```

You can use `LFN_DB` variable to specify the path of database to whereever you want.

## Warning

1. This program is experimental, I got it to work and play with it a little bit, that's how much I tested.

2. Caveats [written in here](https://github.com/python-llfuse/python-llfuse/blob/efae82fd9f600f2ed25e6f3014c5993d64b4ddbf/examples/passthroughfs.py#L9-L41) applies to this program (I modified this code I linked to do what I want)

3. The fuse library this program uses ([python-llfuse](https://github.com/python-llfuse/python-llfuse)) is no longer developed.

## Why not longer filename??

The is a limitation set in [Linux kernel](https://github.com/torvalds/linux/blob/825a0714d2b3883d4f8ff64f6933fb73ee3f1834/fs/fuse/fuse_i.h#L45), if you compile your own kernel you can overcome this limit.