/* BlueHead native application entry point. Apache-2.0. */
#include "application_host.hpp"
int wmain(int count, wchar_t **values) {
  try {
    bh::Arguments args(count, values);
    SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    AddDllDirectory(args.assets.c_str());
    AddDllDirectory(args.assets.parent_path().parent_path().c_str());
    if (args.mapping)
      return bh::ldac_worker(args.mapping, args.request, args.done, args.assets);
    if (args.rpc.empty())
      args.rpc = bh::pipe_name();
    bh::Core core(args.assets, args.offline);
    bh::RpcServer server(core, args.assets, args.rpc);
    if (args.engine) {
      bh::Host host(server, args);
      return host.run();
    }
    bh::Handle parent(args.parent ? OpenProcess(SYNCHRONIZE, FALSE, args.parent) : nullptr);
    while (!server.exited()) {
      if (parent && WaitForSingleObject(parent, 100) == WAIT_OBJECT_0)
        break;
      std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "Native runtime: " << e.what() << '\n';
    return 2;
  }
}
