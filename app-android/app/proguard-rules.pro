# ===== BidKing 远程助手 keep 规则（2026-08-30 开启 R8）=====
# App 反射使用极少，大部分依赖（appcompat/security-crypto/tink）自带 consumer 规则。
# 如开启后出现运行时异常（ClassNotFound/NoSuchMethod），把对应类加到这里并注明原因。

# 崩溃堆栈保留行号（混淆后仍可排查问题）
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# org.json 为平台内置，无需 keep；EncryptedSharedPreferences/security-crypto 自带 consumer 规则。
# Please add these rules to your existing keep rules in order to suppress warnings.
# This is generated automatically by the Android Gradle plugin.
-dontwarn javax.annotation.Nullable
-dontwarn javax.annotation.concurrent.GuardedBy