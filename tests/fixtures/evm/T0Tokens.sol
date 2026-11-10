// SPDX-License-Identifier: MIT
pragma solidity 0.8.35;

/// @notice Executable test-only ERC-20 transfer semantics for Conix event indexing.
contract ConixToken20 {
    string public constant name = "Conix fixture token";
    string public constant symbol = "C20";
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    constructor() {
        totalSupply = 1_000;
        balanceOf[msg.sender] = totalSupply;
        emit Transfer(address(0), msg.sender, totalSupply);
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 remaining = allowance[from][msg.sender];
        require(remaining >= amount, "allowance");
        if (remaining != type(uint256).max) {
            allowance[from][msg.sender] = remaining - amount;
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) private {
        require(to != address(0), "recipient");
        require(balanceOf[from] >= amount, "balance");
        unchecked {
            balanceOf[from] -= amount;
            balanceOf[to] += amount;
        }
        emit Transfer(from, to, amount);
    }
}

interface ConixERC721Receiver {
    function onERC721Received(address operator, address from, uint256 tokenId, bytes calldata data)
        external returns (bytes4);
}

/// @notice Executable test-only ERC-721 ownership and Transfer event semantics.
contract ConixToken721 {
    string public constant name = "Conix fixture NFT";
    string public constant symbol = "C721";

    mapping(uint256 => address) private owners;
    mapping(address => uint256) private balances;
    mapping(uint256 => address) private approvals;
    mapping(address => mapping(address => bool)) private operators;

    event Transfer(address indexed from, address indexed to, uint256 indexed tokenId);
    event Approval(address indexed owner, address indexed approved, uint256 indexed tokenId);
    event ApprovalForAll(address indexed owner, address indexed operator, bool approved);

    constructor() {
        owners[7] = msg.sender;
        balances[msg.sender] = 1;
        emit Transfer(address(0), msg.sender, 7);
    }

    function supportsInterface(bytes4 id) external pure returns (bool) {
        return id == 0x01ffc9a7 || id == 0x80ac58cd || id == 0x5b5e139f;
    }

    function balanceOf(address owner) external view returns (uint256) {
        require(owner != address(0), "owner");
        return balances[owner];
    }

    function ownerOf(uint256 tokenId) public view returns (address) {
        address owner = owners[tokenId];
        require(owner != address(0), "token");
        return owner;
    }

    function getApproved(uint256 tokenId) external view returns (address) {
        ownerOf(tokenId);
        return approvals[tokenId];
    }

    function isApprovedForAll(address owner, address operator) external view returns (bool) {
        return operators[owner][operator];
    }

    function approve(address approved, uint256 tokenId) external {
        address owner = ownerOf(tokenId);
        require(msg.sender == owner || operators[owner][msg.sender], "authority");
        approvals[tokenId] = approved;
        emit Approval(owner, approved, tokenId);
    }

    function setApprovalForAll(address operator, bool approved) external {
        require(operator != msg.sender, "self");
        operators[msg.sender][operator] = approved;
        emit ApprovalForAll(msg.sender, operator, approved);
    }

    function transferFrom(address from, address to, uint256 tokenId) public {
        address owner = ownerOf(tokenId);
        require(owner == from && to != address(0), "owner or recipient");
        require(
            msg.sender == owner || approvals[tokenId] == msg.sender || operators[owner][msg.sender],
            "authority"
        );
        delete approvals[tokenId];
        unchecked {
            balances[from] -= 1;
            balances[to] += 1;
        }
        owners[tokenId] = to;
        emit Transfer(from, to, tokenId);
    }

    function safeTransferFrom(address from, address to, uint256 tokenId) external {
        safeTransferFrom(from, to, tokenId, "");
    }

    function safeTransferFrom(address from, address to, uint256 tokenId, bytes memory data) public {
        transferFrom(from, to, tokenId);
        if (to.code.length != 0) {
            require(
                ConixERC721Receiver(to).onERC721Received(msg.sender, from, tokenId, data)
                    == ConixERC721Receiver.onERC721Received.selector,
                "receiver"
            );
        }
    }
}
