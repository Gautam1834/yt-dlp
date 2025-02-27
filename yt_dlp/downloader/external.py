from __future__ import unicode_literals

import os.path
import re
import subprocess
import sys
import time

try:
    from Crypto.Cipher import AES
    can_decrypt_frag = True
except ImportError:
    can_decrypt_frag = False

from .common import FileDownloader
from ..compat import (
    compat_setenv,
    compat_str,
)
from ..postprocessor.ffmpeg import FFmpegPostProcessor, EXT_TO_OUT_FORMATS
from ..utils import (
    cli_option,
    cli_valueless_option,
    cli_bool_option,
    cli_configuration_args,
    encodeFilename,
    error_to_compat_str,
    encodeArgument,
    handle_youtubedl_headers,
    check_executable,
    is_outdated_version,
    process_communicate_or_kill,
    sanitized_Request,
    sanitize_open,
)


class ExternalFD(FileDownloader):
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps')

    def real_download(self, filename, info_dict):
        self.report_destination(filename)
        tmpfilename = self.temp_name(filename)

        try:
            started = time.time()
            retval = self._call_downloader(tmpfilename, info_dict)
        except KeyboardInterrupt:
            if not info_dict.get('is_live'):
                raise
            # Live stream downloading cancellation should be considered as
            # correct and expected termination thus all postprocessing
            # should take place
            retval = 0
            self.to_screen('[%s] Interrupted by user' % self.get_basename())

        if retval == 0:
            status = {
                'filename': filename,
                'status': 'finished',
                'elapsed': time.time() - started,
            }
            if filename != '-':
                fsize = os.path.getsize(encodeFilename(tmpfilename))
                self.to_screen('\r[%s] Downloaded %s bytes' % (self.get_basename(), fsize))
                self.try_rename(tmpfilename, filename)
                status.update({
                    'downloaded_bytes': fsize,
                    'total_bytes': fsize,
                })
            self._hook_progress(status)
            return True
        else:
            self.to_stderr('\n')
            self.report_error('%s exited with code %d' % (
                self.get_basename(), retval))
            return False

    @classmethod
    def get_basename(cls):
        return cls.__name__[:-2].lower()

    @property
    def exe(self):
        return self.params.get('external_downloader')

    @classmethod
    def available(cls, path=None):
        return check_executable(path or cls.get_basename(), [cls.AVAILABLE_OPT])

    @classmethod
    def supports(cls, info_dict):
        return info_dict['protocol'] in cls.SUPPORTED_PROTOCOLS

    @classmethod
    def can_download(cls, info_dict, path=None):
        return cls.available(path) and cls.supports(info_dict)

    def _option(self, command_option, param):
        return cli_option(self.params, command_option, param)

    def _bool_option(self, command_option, param, true_value='true', false_value='false', separator=None):
        return cli_bool_option(self.params, command_option, param, true_value, false_value, separator)

    def _valueless_option(self, command_option, param, expected_value=True):
        return cli_valueless_option(self.params, command_option, param, expected_value)

    def _configuration_args(self, *args, **kwargs):
        return cli_configuration_args(
            self.params.get('external_downloader_args'),
            self.get_basename(), *args, **kwargs)

    def _call_downloader(self, tmpfilename, info_dict):
        """ Either overwrite this or implement _make_cmd """
        cmd = [encodeArgument(a) for a in self._make_cmd(tmpfilename, info_dict)]

        self._debug_cmd(cmd)

        p = subprocess.Popen(
            cmd, stderr=subprocess.PIPE)
        _, stderr = process_communicate_or_kill(p)
        if p.returncode != 0:
            self.to_stderr(stderr.decode('utf-8', 'replace'))

        if 'fragments' in info_dict:
            file_list = []
            dest, _ = sanitize_open(tmpfilename, 'wb')
            for [i, fragment] in enumerate(info_dict['fragments']):
                file = '%s_%s.frag' % (tmpfilename, i)
                decrypt_info = fragment.get('decrypt_info')
                src, _ = sanitize_open(file, 'rb')
                if decrypt_info:
                    if decrypt_info['METHOD'] == 'AES-128':
                        iv = decrypt_info.get('IV')
                        decrypt_info['KEY'] = decrypt_info.get('KEY') or self.ydl.urlopen(
                            self._prepare_url(info_dict, info_dict.get('_decryption_key_url') or decrypt_info['URI'])).read()
                        encrypted_data = src.read()
                        decrypted_data = AES.new(
                            decrypt_info['KEY'], AES.MODE_CBC, iv).decrypt(encrypted_data)
                        dest.write(decrypted_data)
                    else:
                        fragment_data = src.read()
                        dest.write(fragment_data)
                else:
                    fragment_data = src.read()
                    dest.write(fragment_data)
                src.close()
                file_list.append(file)
            dest.close()
            if not self.params.get('keep_fragments', False):
                for file_path in file_list:
                    try:
                        os.remove(file_path)
                    except OSError as ose:
                        self.report_error("Unable to delete file %s; %s" % (file_path, error_to_compat_str(ose)))
                try:
                    file_path = '%s.frag.urls' % tmpfilename
                    os.remove(file_path)
                except OSError as ose:
                    self.report_error("Unable to delete file %s; %s" % (file_path, error_to_compat_str(ose)))

        return p.returncode

    def _prepare_url(self, info_dict, url):
        headers = info_dict.get('http_headers')
        return sanitized_Request(url, None, headers) if headers else url


class CurlFD(ExternalFD):
    AVAILABLE_OPT = '-V'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '--location', '-o', tmpfilename]
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', '%s: %s' % (key, val)]

        cmd += self._bool_option('--continue-at', 'continuedl', '-', '0')
        cmd += self._valueless_option('--silent', 'noprogress')
        cmd += self._valueless_option('--verbose', 'verbose')
        cmd += self._option('--limit-rate', 'ratelimit')
        retry = self._option('--retry', 'retries')
        if len(retry) == 2:
            if retry[1] in ('inf', 'infinite'):
                retry[1] = '2147483647'
            cmd += retry
        cmd += self._option('--max-filesize', 'max_filesize')
        cmd += self._option('--interface', 'source_address')
        cmd += self._option('--proxy', 'proxy')
        cmd += self._valueless_option('--insecure', 'nocheckcertificate')
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd

    def _call_downloader(self, tmpfilename, info_dict):
        cmd = [encodeArgument(a) for a in self._make_cmd(tmpfilename, info_dict)]

        self._debug_cmd(cmd)

        # curl writes the progress to stderr so don't capture it.
        p = subprocess.Popen(cmd)
        process_communicate_or_kill(p)
        return p.returncode


class AxelFD(ExternalFD):
    AVAILABLE_OPT = '-V'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-o', tmpfilename]
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['-H', '%s: %s' % (key, val)]
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd


class WgetFD(ExternalFD):
    AVAILABLE_OPT = '--version'

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-O', tmpfilename, '-nv', '--no-cookies']
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', '%s: %s' % (key, val)]
        cmd += self._option('--limit-rate', 'ratelimit')
        retry = self._option('--tries', 'retries')
        if len(retry) == 2:
            if retry[1] in ('inf', 'infinite'):
                retry[1] = '0'
            cmd += retry
        cmd += self._option('--bind-address', 'source_address')
        cmd += self._option('--proxy', 'proxy')
        cmd += self._valueless_option('--no-check-certificate', 'nocheckcertificate')
        cmd += self._configuration_args()
        cmd += ['--', info_dict['url']]
        return cmd


class Aria2cFD(ExternalFD):
    AVAILABLE_OPT = '-v'
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps', 'frag_urls')

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = [self.exe, '-c']
        dn = os.path.dirname(tmpfilename)
        if 'fragments' not in info_dict:
            cmd += ['--out', os.path.basename(tmpfilename)]
        verbose_level_args = ['--console-log-level=warn', '--summary-interval=0']
        cmd += self._configuration_args(['--file-allocation=none', '-x16', '-j16', '-s16'] + verbose_level_args)
        if dn:
            cmd += ['--dir', dn]
        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['--header', '%s: %s' % (key, val)]
        cmd += self._option('--interface', 'source_address')
        cmd += self._option('--all-proxy', 'proxy')
        cmd += self._bool_option('--check-certificate', 'nocheckcertificate', 'false', 'true', '=')
        cmd += self._bool_option('--remote-time', 'updatetime', 'true', 'false', '=')
        cmd += ['--auto-file-renaming=false']
        if 'fragments' in info_dict:
            cmd += verbose_level_args
            cmd += ['--uri-selector', 'inorder', '--download-result=hide']
            url_list_file = '%s.frag.urls' % tmpfilename
            url_list = []
            for [i, fragment] in enumerate(info_dict['fragments']):
                tmpsegmentname = '%s_%s.frag' % (os.path.basename(tmpfilename), i)
                url_list.append('%s\n\tout=%s' % (fragment['url'], tmpsegmentname))
            stream, _ = sanitize_open(url_list_file, 'wb')
            stream.write('\n'.join(url_list).encode('utf-8'))
            stream.close()

            cmd += ['-i', url_list_file]
        else:
            cmd += ['--', info_dict['url']]
        return cmd


class HttpieFD(ExternalFD):
    @classmethod
    def available(cls, path=None):
        return check_executable(path or 'http', ['--version'])

    def _make_cmd(self, tmpfilename, info_dict):
        cmd = ['http', '--download', '--output', tmpfilename, info_dict['url']]

        if info_dict.get('http_headers') is not None:
            for key, val in info_dict['http_headers'].items():
                cmd += ['%s:%s' % (key, val)]
        return cmd


class FFmpegFD(ExternalFD):
    SUPPORTED_PROTOCOLS = ('http', 'https', 'ftp', 'ftps', 'm3u8', 'rtsp', 'rtmp', 'mms')

    @classmethod
    def available(cls, path=None):  # path is ignored for ffmpeg
        return FFmpegPostProcessor().available

    def _call_downloader(self, tmpfilename, info_dict):
        url = info_dict['url']
        ffpp = FFmpegPostProcessor(downloader=self)
        if not ffpp.available:
            self.report_error('m3u8 download detected but ffmpeg could not be found. Please install')
            return False
        ffpp.check_version()

        args = [ffpp.executable, '-y']

        for log_level in ('quiet', 'verbose'):
            if self.params.get(log_level, False):
                args += ['-loglevel', log_level]
                break

        seekable = info_dict.get('_seekable')
        if seekable is not None:
            # setting -seekable prevents ffmpeg from guessing if the server
            # supports seeking(by adding the header `Range: bytes=0-`), which
            # can cause problems in some cases
            # https://github.com/ytdl-org/youtube-dl/issues/11800#issuecomment-275037127
            # http://trac.ffmpeg.org/ticket/6125#comment:10
            args += ['-seekable', '1' if seekable else '0']

        args += self._configuration_args()

        # start_time = info_dict.get('start_time') or 0
        # if start_time:
        #     args += ['-ss', compat_str(start_time)]
        # end_time = info_dict.get('end_time')
        # if end_time:
        #     args += ['-t', compat_str(end_time - start_time)]

        if info_dict.get('http_headers') is not None and re.match(r'^https?://', url):
            # Trailing \r\n after each HTTP header is important to prevent warning from ffmpeg/avconv:
            # [http @ 00000000003d2fa0] No trailing CRLF found in HTTP header.
            headers = handle_youtubedl_headers(info_dict['http_headers'])
            args += [
                '-headers',
                ''.join('%s: %s\r\n' % (key, val) for key, val in headers.items())]

        env = None
        proxy = self.params.get('proxy')
        if proxy:
            if not re.match(r'^[\da-zA-Z]+://', proxy):
                proxy = 'http://%s' % proxy

            if proxy.startswith('socks'):
                self.report_warning(
                    '%s does not support SOCKS proxies. Downloading is likely to fail. '
                    'Consider adding --hls-prefer-native to your command.' % self.get_basename())

            # Since December 2015 ffmpeg supports -http_proxy option (see
            # http://git.videolan.org/?p=ffmpeg.git;a=commit;h=b4eb1f29ebddd60c41a2eb39f5af701e38e0d3fd)
            # We could switch to the following code if we are able to detect version properly
            # args += ['-http_proxy', proxy]
            env = os.environ.copy()
            compat_setenv('HTTP_PROXY', proxy, env=env)
            compat_setenv('http_proxy', proxy, env=env)

        protocol = info_dict.get('protocol')

        if protocol == 'rtmp':
            player_url = info_dict.get('player_url')
            page_url = info_dict.get('page_url')
            app = info_dict.get('app')
            play_path = info_dict.get('play_path')
            tc_url = info_dict.get('tc_url')
            flash_version = info_dict.get('flash_version')
            live = info_dict.get('rtmp_live', False)
            conn = info_dict.get('rtmp_conn')
            if player_url is not None:
                args += ['-rtmp_swfverify', player_url]
            if page_url is not None:
                args += ['-rtmp_pageurl', page_url]
            if app is not None:
                args += ['-rtmp_app', app]
            if play_path is not None:
                args += ['-rtmp_playpath', play_path]
            if tc_url is not None:
                args += ['-rtmp_tcurl', tc_url]
            if flash_version is not None:
                args += ['-rtmp_flashver', flash_version]
            if live:
                args += ['-rtmp_live', 'live']
            if isinstance(conn, list):
                for entry in conn:
                    args += ['-rtmp_conn', entry]
            elif isinstance(conn, compat_str):
                args += ['-rtmp_conn', conn]

        args += ['-i', url, '-c', 'copy']

        if self.params.get('test', False):
            args += ['-fs', compat_str(self._TEST_FILE_SIZE)]

        if protocol in ('m3u8', 'm3u8_native'):
            use_mpegts = (tmpfilename == '-') or self.params.get('hls_use_mpegts')
            if use_mpegts is None:
                use_mpegts = info_dict.get('is_live')
            if use_mpegts:
                args += ['-f', 'mpegts']
            else:
                args += ['-f', 'mp4']
                if (ffpp.basename == 'ffmpeg' and is_outdated_version(ffpp._versions['ffmpeg'], '3.2', False)) and (not info_dict.get('acodec') or info_dict['acodec'].split('.')[0] in ('aac', 'mp4a')):
                    args += ['-bsf:a', 'aac_adtstoasc']
        elif protocol == 'rtmp':
            args += ['-f', 'flv']
        else:
            args += ['-f', EXT_TO_OUT_FORMATS.get(info_dict['ext'], info_dict['ext'])]

        args = [encodeArgument(opt) for opt in args]
        args.append(encodeFilename(ffpp._ffmpeg_filename_argument(tmpfilename), True))

        self._debug_cmd(args)

        proc = subprocess.Popen(args, stdin=subprocess.PIPE, env=env)
        try:
            retval = proc.wait()
        except BaseException as e:
            # subprocces.run would send the SIGKILL signal to ffmpeg and the
            # mp4 file couldn't be played, but if we ask ffmpeg to quit it
            # produces a file that is playable (this is mostly useful for live
            # streams). Note that Windows is not affected and produces playable
            # files (see https://github.com/ytdl-org/youtube-dl/issues/8300).
            if isinstance(e, KeyboardInterrupt) and sys.platform != 'win32':
                process_communicate_or_kill(proc, b'q')
            else:
                proc.kill()
                proc.wait()
            raise
        return retval


class AVconvFD(FFmpegFD):
    pass


_BY_NAME = dict(
    (klass.get_basename(), klass)
    for name, klass in globals().items()
    if name.endswith('FD') and name != 'ExternalFD'
)


def list_external_downloaders():
    return sorted(_BY_NAME.keys())


def get_external_downloader(external_downloader):
    """ Given the name of the executable, see whether we support the given
        downloader . """
    # Drop .exe extension on Windows
    bn = os.path.splitext(os.path.basename(external_downloader))[0]
    return _BY_NAME[bn]
