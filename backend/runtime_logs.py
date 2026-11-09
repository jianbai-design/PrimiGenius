"""Incremental timestamp/occurrence cursor for Docker-compatible log streams."""
import codecs
import datetime
import threading


class LogCursor:
    def __init__(self):
        self.timestamp = ''
        self.occurrence = 0
        self.lock = threading.Lock()

    def read(self, container, emit, *, follow=False, stop=None, stream_ref=None, tail=None):
        # Only the boundary timestamp is retained; repeated message text is data.
        with self.lock:
            since = None
            boundary, skip = self.timestamp, self.occurrence
            if boundary:
                since = int(datetime.datetime.fromisoformat(boundary.replace('Z', '+00:00')).timestamp())
            options = dict(stream=True, follow=follow, timestamps=True,
                           stdout=True, stderr=True, since=since)
            if tail is not None and not boundary:
                options['tail'] = tail
            stream = container.logs(**options)
            if stream_ref is not None:
                stream_ref[0] = stream
            decoder = codecs.getincrementaldecoder('utf-8')('replace')
            fragments = []
            boundary_seen = 0

            def accept(record):
                nonlocal boundary_seen
                stamp, separator, line = record.partition(' ')
                if not separator or 'T' not in stamp:
                    raise ValueError('Engine returned a log record without a timestamp: ' + repr(record[:120]))
                if stamp < boundary:
                    return
                if stamp == boundary:
                    boundary_seen += 1
                    if boundary_seen <= skip:
                        return
                # Advance only after the sink has accepted/persisted this record.
                emit(line.rstrip('\r'))
                if stamp == self.timestamp:
                    self.occurrence += 1
                else:
                    self.timestamp, self.occurrence = stamp, 1

            try:
                for chunk in stream:
                    if stop is not None and stop.is_set():
                        return
                    text = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
                    # Split once per chunk, join once per complete record. Repeated
                    # prefix concatenation/suffix copying is quadratic for long logs.
                    parts = text.split('\n')
                    if parts[0]:
                        fragments.append(parts[0])
                    if len(parts) > 1:
                        accept(''.join(fragments))
                        fragments.clear()
                        for line in parts[1:-1]:
                            accept(line)
                        if parts[-1]:
                            fragments.append(parts[-1])
                tail = decoder.decode(b'', final=True)
                if tail:
                    fragments.append(tail)
                if fragments:
                    accept(''.join(fragments))
            finally:
                try:
                    stream.close()
                finally:
                    if stream_ref is not None:
                        stream_ref[0] = None
