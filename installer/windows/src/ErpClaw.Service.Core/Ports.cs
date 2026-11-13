// The free-port draw on 127.0.0.1 (INSTALLER 2.2: a port chosen free at each start).
using System.Net;
using System.Net.Sockets;

namespace ErpClaw.Service.Core;

public interface IPortProbe
{
    /// <summary>True when 127.0.0.1:<paramref name="port"/> can be bound now.</summary>
    bool IsFree(int port);

    /// <summary>A port the system reports free on 127.0.0.1.</summary>
    int DrawFree();
}

/// <summary>The real probe: binds a listener on 127.0.0.1 (exclusively on Windows) and lets it go.</summary>
public sealed class LoopbackPortProbe : IPortProbe
{
    public bool IsFree(int port)
    {
        try
        {
            using var socket = Bind(port);
            return true;
        }
        catch (SocketException)
        {
            return false;
        }
    }

    public int DrawFree()
    {
        using var socket = Bind(0);
        return ((IPEndPoint)socket.LocalEndPoint!).Port;
    }

    private static Socket Bind(int port)
    {
        var socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
        try
        {
            // On Windows an exclusive bind also fails when another account holds the port with SO_REUSEADDR.
            if (OperatingSystem.IsWindows()) socket.ExclusiveAddressUse = true;
            socket.Bind(new IPEndPoint(IPAddress.Loopback, port));
            socket.Listen(1);
            return socket;
        }
        catch
        {
            socket.Dispose();
            throw;
        }
    }
}

public static class PortChoice
{
    /// <summary>The previous port when it is still free, else a new one (2.3 item 4).</summary>
    public static int Choose(int? previous, IPortProbe probe) =>
        previous is int port && probe.IsFree(port) ? port : probe.DrawFree();
}
