"""One compatibility-preserving conversion for polling and streaming stats."""


def container_metrics(stats):
    cpu = stats.get('cpu_stats') or {}
    previous = stats.get('precpu_stats') or {}
    cpu_delta = (cpu.get('cpu_usage') or {}).get('total_usage', 0) - (previous.get('cpu_usage') or {}).get('total_usage', 0)
    system_delta = cpu.get('system_cpu_usage', 0) - previous.get('system_cpu_usage', 0)
    cpus = cpu.get('online_cpus', 1) or 1
    percent = round(cpu_delta / system_delta * cpus * 100, 1) if system_delta > 0 and cpu_delta > 0 else 0.0
    memory = stats.get('memory_stats') or {}
    usage, limit = memory.get('usage', 0), memory.get('limit', 0)
    networks = stats.get('networks') or {}
    interfaces = networks.values() if isinstance(networks, dict) else []
    rx = tx = read = written = 0
    for interface in interfaces:
        rx += interface.get('rx_bytes', 0)
        tx += interface.get('tx_bytes', 0)
    for entry in (stats.get('blkio_stats') or {}).get('io_service_bytes_recursive') or []:
        if entry.get('op') == 'read':
            read += entry.get('value', 0)
        elif entry.get('op') == 'write':
            written += entry.get('value', 0)
    return dict(cpu_percent=percent, mem_usage_mb=round(usage / 2**20, 1) if usage else 0,
                mem_limit_mb=round(limit / 2**20, 1) if limit else 0,
                mem_percent=round(usage / limit * 100, 1) if limit > 0 else 0.0,
                net_rx_bytes=rx, net_tx_bytes=tx, blk_read_bytes=read, blk_write_bytes=written,
                pids=(stats.get('pids_stats') or {}).get('current', 0), num_cpus=cpus)
