#include "catalog.hpp"
#include <iostream>
int wmain(int count, wchar_t **args) {
  try {
    bh::require(count == 4, "assets/backend/facts required");
    bh::firmware::Catalog catalog(args[1]);
    auto profile = catalog.select(bh::utf8(args[2]), bh::json::parse(bh::utf8(args[3])));
    std::cout << profile.dump() << '\n';
    return 0;
  } catch (const std::exception &e) {
    std::cerr << e.what();
    return 2;
  }
}
